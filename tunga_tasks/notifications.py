import datetime
import time

from django.contrib.auth import get_user_model
from django.db.models import When, Case, IntegerField
from django.db.models.aggregates import Sum
from django.db.models.expressions import F
from django.template.defaultfilters import truncatewords, floatformat
from django_rq.decorators import job

from tunga.settings import TUNGA_URL, TUNGA_STAFF_UPDATE_EMAIL_RECIPIENTS, SLACK_ATTACHMENT_COLOR_TUNGA, \
    SLACK_ATTACHMENT_COLOR_RED, SLACK_ATTACHMENT_COLOR_GREEN, SLACK_ATTACHMENT_COLOR_NEUTRAL, \
    SLACK_ATTACHMENT_COLOR_BLUE, SLACK_DEVELOPER_INCOMING_WEBHOOK, SLACK_STAFF_INCOMING_WEBHOOK, \
    SLACK_STAFF_UPDATES_CHANNEL, SLACK_DEVELOPER_UPDATES_CHANNEL, SLACK_PMS_UPDATES_CHANNEL, \
    MAILCHIMP_NEW_USER_AUTOMATION_WORKFLOW_ID, MAILCHIMP_NEW_USER_AUTOMATION_EMAIL_ID, \
    TUNGA_STAFF_LOW_LEVEL_UPDATE_EMAIL_RECIPIENTS
from tunga_auth.filterbackends import my_connections_q_filter
from tunga_tasks import slugs
from tunga_tasks.models import Task, Participation, Application, ProgressEvent, ProgressReport, Quote, Estimate
from tunga_tasks.utils import get_task_integration
from tunga_utils import slack_utils, mailchimp_utils
from tunga_utils.constants import USER_TYPE_DEVELOPER, VISIBILITY_DEVELOPER, VISIBILITY_MY_TEAM, TASK_SCOPE_TASK, \
    USER_TYPE_PROJECT_MANAGER, TASK_SOURCE_NEW_USER, STATUS_INITIAL, STATUS_SUBMITTED, STATUS_APPROVED, STATUS_DECLINED, \
    STATUS_ACCEPTED, STATUS_REJECTED, PROGRESS_EVENT_TYPE_PM, PROGRESS_EVENT_TYPE_CLIENT, \
    PROGRESS_REPORT_STATUS_BEHIND_AND_STUCK, APP_INTEGRATION_PROVIDER_SLACK

from tunga_utils.emails import send_mail
from tunga_utils.helpers import clean_instance, convert_to_text
from tunga_utils.slack_utils import get_user_im_id


@job
def possibly_trigger_schedule_call_automation(instance, wait=15*60):
    # Wait for user to possibly schedule a call
    time.sleep(wait)

    instance = clean_instance(isinstance(instance, Task) and instance.id or instance, Task)  # needs to be refreshed
    if not instance.schedule_call_start:
        # Make sure user is in mailing list
        mailchimp_utils.subscribe_new_user(
            instance.user.email, **dict(FNAME=instance.user.first_name, LNAME=instance.user.last_name)
        )

        # Trigger email from automation
        mailchimp_utils.add_email_to_automation_queue(
            email_address=instance.user.email,
            workflow_id=MAILCHIMP_NEW_USER_AUTOMATION_WORKFLOW_ID,
            email_id=MAILCHIMP_NEW_USER_AUTOMATION_EMAIL_ID
        )


def create_task_slack_msg(task, summary='', channel='#general', show_schedule=True, show_contacts=False):
    task_url = '{}/work/{}/'.format(TUNGA_URL, task.id)
    attachments = [
        {
            slack_utils.KEY_TITLE: task.summary,
            slack_utils.KEY_TITLE_LINK: task_url,
            slack_utils.KEY_TEXT: task.excerpt,
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_TUNGA
        }
    ]
    extra_details = ''
    if task.type:
        extra_details += '*Type*: {}\n'.format(task.get_type_display())
    if task.skills:
        extra_details += '*Skills*: {}\n'.format(task.skills_list)
    if task.deadline:
        extra_details += '*Deadline*: {}\n'.format(task.deadline.strftime("%d %b, %Y"))
    if task.fee:
        amount = task.is_developer_ready and task.pay_dev or task.pay
        extra_details += '*Fee*: EUR {}\n'.format(floatformat(amount, arg=-2))
    if show_schedule and task.schedule_call_start:
        extra_details += '*Available*: \nDate: {}\nTime: {} {} UTC\n'.format(
            task.schedule_call_start.strftime("%d %b, %Y"),
            task.schedule_call_start.strftime("%I:%M%p"),
            task.schedule_call_end and ' - {}'.format(task.schedule_call_end.strftime("%I:%M%p")) or ''
        )
    if show_contacts:
        extra_details += '*Email*: {}\n'.format(task.user.email)
        if task.skype_id:
            extra_details += '*Skype ID or Call URL*: {}\n'.format(task.skype_id)
    if extra_details:
        attachments.append({
            slack_utils.KEY_TEXT: extra_details,
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_GREEN
        })
    if task.deliverables:
        attachments.append({
            slack_utils.KEY_TITLE: 'Deliverables',
            slack_utils.KEY_TEXT: task.deliverables,
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_BLUE
        })
    if task.stack_description:
        attachments.append({
            slack_utils.KEY_TITLE: 'Tech Stack',
            slack_utils.KEY_TEXT: task.stack_description,
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_NEUTRAL
        })
    if not summary:
        summary = "New {} created by {} | <{}|View on Tunga>".format(
            task.scope == TASK_SCOPE_TASK and 'task' or 'project',
            task.user.display_name, task_url)
    return {
        slack_utils.KEY_TEXT: summary,
        slack_utils.KEY_CHANNEL: channel,
        slack_utils.KEY_ATTACHMENTS: attachments
    }


@job
def notify_new_task(instance, new_user=False):
    notify_new_task_client_receipt_email(instance)

    if not new_user:
        # Task from new users need to be qualified before they get to the community
        notify_new_task_community(instance)

    notify_new_task_admin(instance, new_user=new_user)


@job
def notify_task_approved(instance, new_user=False):
    notify_new_task_client_receipt_email(instance)
    notify_new_task_admin(instance, new_user=new_user, completed=True)
    notify_new_task_community(instance)


@job
def notify_new_task_client_receipt_email(instance, new_user=False, reminder=False):
    instance = clean_instance(instance, Task)

    subject = "Your {} has been posted on Tunga".format(
        instance.scope == TASK_SCOPE_TASK and 'task' or 'project'
    )
    if instance.is_task and not instance.approved:
        subject = "{}Finalize your {}".format(
            reminder and 'Reminder: ' or '',
            instance.scope == TASK_SCOPE_TASK and 'task' or 'project'
        )
    to = [instance.user.email]
    if instance.owner:
        to.append(instance.owner.email)

    ctx = {
        'owner': instance.owner or instance.user,
        'task': instance,
        'task_url': '%s/task/%s/' % (TUNGA_URL, instance.id),
        'task_edit_url': '%s/task/%s/edit/complete-task/' % (TUNGA_URL, instance.id)
    }

    if instance.source == TASK_SOURCE_NEW_USER and not instance.user.is_confirmed:
        url_prefix = '{}/reset-password/confirm/{}/{}?new_user=true&next='.format(
            TUNGA_URL, instance.user.uid, instance.user.generate_reset_token()
        )
        ctx['task_url'] = '{}{}'.format(url_prefix, ctx['task_url'])
        ctx['task_edit_url'] = '{}{}'.format(url_prefix, ctx['task_edit_url'])

    if instance.is_task:
        if instance.approved:
            email_template = 'tunga/email/email_new_task_client_approved'
        else:
            if reminder:
                email_template = 'tunga/email/email_new_task_client_more_info_reminder'
            else:
                email_template = 'tunga/email/email_new_task_client_more_info'
    else:
        email_template = 'tunga/email/email_new_task_client_approved'
    if send_mail(subject, email_template, to, ctx, **dict(deal_ids=[instance.hubspot_deal_id])):
        if not instance.approved:
            instance.complete_task_email_at = datetime.datetime.utcnow()
            if reminder:
                instance.reminded_complete_task = True
            instance.save()


@job
def notify_new_task_admin(instance, new_user=False, completed=False, call_scheduled=False):
    notify_new_task_admin_slack(instance, new_user=new_user, completed=completed, call_scheduled=call_scheduled)
    if not (completed or call_scheduled):
        # Only initial task creation will be reported via email
        notify_new_task_admin_email(instance, new_user=new_user, completed=completed, call_scheduled=call_scheduled)


@job
def notify_new_task_admin_email(instance, new_user=False, completed=False, call_scheduled=False):
    instance = clean_instance(instance, Task)

    completed_phrase_subject = ''
    completed_phrase_body = ''
    if call_scheduled:
        completed_phrase_subject = 'availability window shared'
        completed_phrase_body = 'shared an availability window'
    elif completed:
        completed_phrase_subject = 'details completed'
        completed_phrase_body = 'completed the details'

    subject = "New{} {} {} by {}{}".format(
        (completed or call_scheduled) and ' wizard' or '',
        instance.scope == TASK_SCOPE_TASK and 'task' or 'project',
        completed_phrase_subject or 'created',
        instance.user.first_name, new_user and ' (New user)' or ''
    )

    to = TUNGA_STAFF_LOW_LEVEL_UPDATE_EMAIL_RECIPIENTS  # Notified via Slack so limit receiving admins
    ctx = {
        'owner': instance.owner or instance.user,
        'task': instance,
        'task_url': '%s/task/%s/' % (TUNGA_URL, instance.id),
        'completed_phrase': completed_phrase_body
    }
    send_mail(subject, 'tunga/email/email_new_task', to, ctx, **dict(deal_ids=[instance.hubspot_deal_id]))


@job
def notify_new_task_admin_slack(instance, new_user=False, completed=False, call_scheduled=False):
    instance = clean_instance(instance, Task)
    task_url = '{}/work/{}/'.format(TUNGA_URL, instance.id)

    completed_phrase = ''
    if call_scheduled:
        completed_phrase = 'availability window shared'
    elif completed:
        completed_phrase = 'details completed'

    summary = "{} {} {} by {}{} | <{}|View on Tunga>".format(
        (completed or call_scheduled) and 'New wizard' or 'New',
        instance.scope == TASK_SCOPE_TASK and 'task' or 'project',
        completed_phrase or 'created',
        instance.user.first_name, new_user and ' (New user)' or '',
        task_url
    )
    slack_msg = create_task_slack_msg(instance, summary=summary, channel=SLACK_STAFF_UPDATES_CHANNEL, show_contacts=True)
    slack_utils.send_incoming_webhook(SLACK_STAFF_INCOMING_WEBHOOK, slack_msg)


@job
def remind_no_task_applications(instance, admin=True):
    remind_no_task_applications_slack(instance, admin=admin)


@job
def remind_no_task_applications_slack(instance, admin=True):
    instance = clean_instance(instance, Task)

    if not instance.is_task:
        return
    task_url = '{}/work/{}/'.format(TUNGA_URL, instance.id)
    new_user = instance.source == TASK_SOURCE_NEW_USER

    summary = "Reminder: No applications yet for {} {} | <{}|View on Tunga>".format(
        instance.scope == TASK_SCOPE_TASK and 'task' or 'project',
        new_user and admin and ' (New user)' or '',
        task_url
    )
    slack_msg = create_task_slack_msg(
        instance, summary=summary,
        channel=admin and SLACK_STAFF_UPDATES_CHANNEL or SLACK_DEVELOPER_UPDATES_CHANNEL,
        show_contacts=admin
    )
    slack_utils.send_incoming_webhook(
        admin and SLACK_STAFF_INCOMING_WEBHOOK or SLACK_DEVELOPER_INCOMING_WEBHOOK,
        slack_msg
    )


@job
def notify_review_task_admin(instance):
    notify_review_task_admin_slack(instance)


@job
def notify_review_task_admin_slack(instance):
    instance = clean_instance(instance, Task)
    task_url = '{}/work/{}/'.format(TUNGA_URL, instance.id)
    new_user = instance.source == TASK_SOURCE_NEW_USER

    summary = "Reminder: Review {} {} | <{}|View on Tunga>\nCreated: {}".format(
        instance.scope == TASK_SCOPE_TASK and 'task' or 'project',
        new_user and ' (New user)' or '',
        task_url,
        instance.created_at.strftime("%d %b, %Y"),
        instance.approved_at and 'Approved: {}'.format(instance.approved_at.strftime("%d %b, %Y")) or '',
    )
    slack_msg = create_task_slack_msg(
        instance, summary=summary,
        channel=SLACK_STAFF_UPDATES_CHANNEL,
        show_contacts=True
    )
    slack_utils.send_incoming_webhook(SLACK_STAFF_INCOMING_WEBHOOK, slack_msg)


@job
def notify_new_task_community(instance):
    notify_new_task_community_email(instance)
    notify_new_task_community_slack(instance)


def get_suggested_community_receivers(instance, user_type=USER_TYPE_DEVELOPER, respect_visibility=True):
    # Filter users based on nature of work
    queryset = get_user_model().objects.filter(
        type=user_type
    )

    # Only developers on client's team
    if respect_visibility and instance.visibility == VISIBILITY_MY_TEAM and user_type == USER_TYPE_DEVELOPER:
        queryset = queryset.filter(
            my_connections_q_filter(instance.user)
        )

    ordering = []

    # Order by matching skills
    task_skills = instance.skills.all()
    if task_skills:
        when = []
        for skill in task_skills:
            new_when = When(
                userprofile__skills=skill,
                then=1
            )
            when.append(new_when)
        queryset = queryset.annotate(matches=Sum(
            Case(
                *when,
                default=0,
                output_field=IntegerField()
            )
        ))
        ordering.append('-matches')

    # Order developers by tasks completed
    if user_type == USER_TYPE_DEVELOPER:
        queryset = queryset.annotate(
            tasks_completed=Sum(
                Case(
                    When(
                        participation__task__closed=True,
                        participation__user__id=F('id'),
                        participation__status=STATUS_ACCEPTED,
                        then=1
                    ),
                    default=0,
                    output_field=IntegerField()
                )
            )
        )
        ordering.append('-tasks_completed')

    if ordering:
        queryset = queryset.order_by(*ordering)
    if queryset:
        return queryset[:15]
    return


@job
def notify_new_task_community_email(instance):
    instance = clean_instance(instance, Task)

    # Notify Devs or PMs
    community_receivers = None
    if instance.is_developer_ready:
        # Notify developers
        if instance.approved and instance.visibility in [VISIBILITY_DEVELOPER, VISIBILITY_MY_TEAM]:
            community_receivers = get_suggested_community_receivers(instance, user_type=USER_TYPE_DEVELOPER)
    elif instance.is_project and not instance.pm:
        community_receivers = get_suggested_community_receivers(instance, user_type=USER_TYPE_PROJECT_MANAGER)

    if instance.is_project and instance.pm:
        community_receivers = [instance.pm]

    subject = "New {} created by {}".format(
        instance.scope == TASK_SCOPE_TASK and 'task' or 'project',
        instance.user.first_name
    )

    if community_receivers:
        to = [community_receivers[0].email]
        bcc = None
        if len(community_receivers) > 1:
            bcc = [user.email for user in community_receivers[1:]] if community_receivers[1:] else None
        ctx = {
            'owner': instance.owner or instance.user,
            'task': instance,
            'task_url': '{}/work/{}/'.format(TUNGA_URL, instance.id)
        }
        send_mail(subject, 'tunga/email/email_new_task', to, ctx, bcc=bcc, **dict(deal_ids=[instance.hubspot_deal_id]))


@job
def notify_new_task_community_slack(instance):
    instance = clean_instance(instance, Task)

    # Notify Devs or PMs via Slack
    if (not instance.is_developer_ready) or (instance.approved and instance.visibility == VISIBILITY_DEVELOPER):
        slack_msg = create_task_slack_msg(
            instance,
            channel=instance.is_developer_ready and SLACK_DEVELOPER_UPDATES_CHANNEL or SLACK_PMS_UPDATES_CHANNEL
        )
        slack_utils.send_incoming_webhook(SLACK_DEVELOPER_INCOMING_WEBHOOK, slack_msg)


VERB_MAP_STATUS_CHANGE = {
    STATUS_SUBMITTED: 'submitted',
    STATUS_APPROVED: 'approved',
    STATUS_DECLINED: 'declined',
    STATUS_ACCEPTED: 'accepted',
    STATUS_REJECTED: 'rejected'
}


@job
def notify_estimate_status_email(instance, estimate_type='estimate', target_admins=False):
    instance = clean_instance(instance, estimate_type == 'quote' and Quote or Estimate)
    if instance.status == STATUS_INITIAL:
        return

    actor = None
    target = None
    action_verb = VERB_MAP_STATUS_CHANGE.get(instance.status, None)
    recipients = None

    if instance.status in [STATUS_SUBMITTED]:
        actor = instance.user
        recipients = TUNGA_STAFF_UPDATE_EMAIL_RECIPIENTS
    elif instance.status in [STATUS_APPROVED, STATUS_DECLINED]:
        actor = instance.moderated_by
        target = instance.user
        recipients = [instance.user.email]
    elif instance.status in [STATUS_ACCEPTED, STATUS_REJECTED]:
        actor = instance.reviewed_by
        if target_admins:
            recipients = TUNGA_STAFF_UPDATE_EMAIL_RECIPIENTS
        else:
            target = instance.user
            recipients = [instance.user.email]

            # Notify staff in a separate email
            notify_estimate_status_email.delay(instance.id, estimate_type=estimate_type, target_admins=True)

    subject = "{} {} {}".format(
        actor.first_name,
        action_verb,
        estimate_type == 'estimate' and 'an estimate' or 'a quote'
    )
    to = recipients

    ctx = {
        'owner': instance.user,
        'estimate': instance,
        'task': instance.task,
        'estimate_url': '{}/work/{}/{}/{}'.format(TUNGA_URL, instance.task.id, estimate_type, instance.id),
        'actor': actor,
        'target': target,
        'verb': action_verb,
        'noun': estimate_type
    }

    if send_mail(
            subject, 'tunga/email/email_estimate_status', to, ctx, **dict(deal_ids=[instance.task.hubspot_deal_id])
    ):
        if instance.status == STATUS_SUBMITTED:
            instance.moderator_email_at = datetime.datetime.utcnow()
            instance.save()
        if instance.status in [STATUS_ACCEPTED, STATUS_REJECTED]:
            instance.reviewed_email_at = datetime.datetime.utcnow()
            instance.save()

    if instance.status == STATUS_APPROVED:
        notify_estimate_approved_client_email(instance, estimate_type=estimate_type)


def notify_estimate_approved_client_email(instance, estimate_type='estimate'):
    instance = clean_instance(instance, estimate_type == 'quote' and Quote or Estimate)
    if instance.status != STATUS_APPROVED:
        return
    subject = "{} submitted {}".format(
        instance.user.first_name,
        estimate_type == 'estimate' and 'an estimate' or 'a quote'
    )
    to = [instance.task.user.email]
    if instance.task.owner:
        to.append(instance.task.owner.email)
    ctx = {
        'owner': instance.user,
        'estimate': instance,
        'task': instance.task,
        'estimate_url': '{}/work/{}/{}/{}'.format(TUNGA_URL, instance.task.id, estimate_type, instance.id),
        'actor': instance.user,
        'target': instance.task.owner or instance.task.user,
        'verb': 'submitted',
        'noun': estimate_type
    }

    if instance.task.source == TASK_SOURCE_NEW_USER and not instance.task.user.is_confirmed:
        url_prefix = '{}/reset-password/confirm/{}/{}?new_user=true&next='.format(
            TUNGA_URL, instance.user.uid, instance.user.generate_reset_token()
        )
        ctx['estimate_url'] = '{}{}'.format(url_prefix, ctx['estimate_url'])

    if send_mail(
            subject, 'tunga/email/email_estimate_status', to, ctx, **dict(deal_ids=[instance.task.hubspot_deal_id])
    ):
        instance.reviewer_email_at = datetime.datetime.utcnow()
        instance.save()


@job
def send_new_task_invitation_email(instance):
    instance = clean_instance(instance, Participation)
    subject = "Task invitation from {}".format(instance.created_by.first_name)
    to = [instance.user.email]
    ctx = {
        'inviter': instance.created_by,
        'invitee': instance.user,
        'task': instance.task,
        'task_url': '%s/work/%s/' % (TUNGA_URL, instance.task.id)
    }
    send_mail(
        subject, 'tunga/email/email_new_task_invitation', to, ctx, **dict(deal_ids=[instance.task.hubspot_deal_id])
    )


@job
def notify_task_invitation_response(instance):
    notify_task_invitation_response_email(instance)
    notify_task_invitation_response_slack(instance)


@job
def notify_task_invitation_response_email(instance):
    instance = clean_instance(instance, Participation)
    if instance.status not in [STATUS_ACCEPTED, STATUS_REJECTED]:
        return

    subject = "Task invitation {} by {}".format(
        instance.status == STATUS_ACCEPTED and 'accepted' or 'rejected', instance.user.first_name
    )
    to = list({instance.task.user.email, instance.created_by.email})
    ctx = {
        'inviter': instance.created_by,
        'invitee': instance.user,
        'accepted': instance.status == STATUS_ACCEPTED,
        'task': instance.task,
        'task_url': '%s/work/%s/' % (TUNGA_URL, instance.task.id)
    }
    send_mail(
        subject, 'tunga/email/email_task_invitation_response', to, ctx, **dict(deal_ids=[instance.task.hubspot_deal_id])
    )


@job
def notify_task_invitation_response_slack(instance):
    instance = clean_instance(instance, Participation)

    if not slack_utils.is_task_notification_enabled(instance.task, slugs.EVENT_APPLICATION):
        return

    task_url = '%s/work/%s/' % (TUNGA_URL, instance.task_id)
    slack_msg = "Task invitation %s by %s %s\n\n<%s|View on Tunga>" % (
        instance.status == STATUS_ACCEPTED and 'accepted' or 'rejected', instance.user.short_name,
        instance.status == STATUS_ACCEPTED and ':smiley: :fireworks:' or ':unamused:',
        task_url
    )
    slack_utils.send_integration_message(instance.task, message=slack_msg)


@job
def notify_new_task_application(instance):
    # Notify project owner
    notify_new_task_application_owner_email(instance)
    notify_new_task_application_slack(instance, admin=False)

    # Send email confirmation to applicant
    confirm_task_application_to_applicant_email.delay(instance.id)

    # Notify admins
    notify_new_task_application_slack.delay(instance.id, admin=True)


@job
def notify_new_task_application_owner_email(instance):
    instance = clean_instance(instance, Application)
    subject = "New application from {}".format(instance.user.short_name)
    to = [instance.task.user.email]
    if instance.task.owner:
        to.append(instance.task.owner.email)
    ctx = {
        'owner': instance.task.owner or instance.task.user,
        'applicant': instance.user,
        'task': instance.task,
        'task_url': '%s/work/%s/applications/' % (TUNGA_URL, instance.task_id)
    }

    if instance.task.source == TASK_SOURCE_NEW_USER and not instance.user.is_confirmed:
        url_prefix = '{}/reset-password/confirm/{}/{}?new_user=true&next='.format(
            TUNGA_URL, instance.user.uid, instance.user.generate_reset_token()
        )
        ctx['task_url'] = '{}{}'.format(url_prefix, ctx['task_url'])
    send_mail(
        subject, 'tunga/email/email_new_task_application', to, ctx, **dict(deal_ids=[instance.task.hubspot_deal_id])
    )


@job
def notify_new_task_application_slack(instance, admin=True):
    instance = clean_instance(instance, Application)

    if not slack_utils.is_task_notification_enabled(instance.task, slugs.EVENT_APPLICATION):
        return

    application_url = '%s/work/%s/applications/' % (TUNGA_URL, instance.task_id)
    slack_msg = "New application from %s" % instance.user.short_name
    attachments = [
        {
            slack_utils.KEY_TITLE: instance.task.summary,
            slack_utils.KEY_TITLE_LINK: application_url,
            slack_utils.KEY_TEXT: '%s%s%s%s\n\n<%s|View on Tunga>' %
                                  (truncatewords(convert_to_text(instance.pitch), 100),
                                   instance.hours_needed and '\n*Workload:* {} hrs'.format(instance.hours_needed) or '',
                                   instance.deliver_at and '\n*Delivery Date:* {}'.format(
                                       instance.deliver_at.strftime("%d %b, %Y")
                                   ) or '',
                                   instance.remarks and '\n*Remarks:* {}'.format(
                                       truncatewords(convert_to_text(instance.remarks), 100)
                                   ) or '',
                                   application_url),
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_TUNGA
        }
    ]
    if admin:
        slack_utils.send_incoming_webhook(
            SLACK_STAFF_INCOMING_WEBHOOK,
            {
                slack_utils.KEY_TEXT: slack_msg,
                slack_utils.KEY_ATTACHMENTS: attachments,
                slack_utils.KEY_CHANNEL: SLACK_STAFF_UPDATES_CHANNEL
            }
        )
    else:
        slack_utils.send_integration_message(instance.task, message=slack_msg, attachments=attachments)


@job
def confirm_task_application_to_applicant_email(instance):
    instance = clean_instance(instance, Application)
    subject = "You applied for a task: {}".format(instance.task.summary)
    to = [instance.user.email]
    ctx = {
        'owner': instance.task.owner or instance.task.user,
        'applicant': instance.user,
        'task': instance.task,
        'task_url': '%s/work/%s/' % (TUNGA_URL, instance.task.id)
    }
    send_mail(
        subject, 'tunga/email/email_new_task_application_applicant', to, ctx,
        **dict(deal_ids=[instance.task.hubspot_deal_id])
    )


@job
def notify_task_application_response(instance):
    # Notify owner
    notify_task_application_response_owner_email(instance)

    # Notify admins
    notify_task_application_response_admin_email(instance)
    notify_task_application_response_slack(instance, admin=True)


@job
def notify_task_application_response_owner_email(instance):
    instance = clean_instance(instance, Application)
    subject = "Task application {}".format(instance.status == STATUS_ACCEPTED and 'accepted' or 'rejected')
    to = [instance.user.email]
    ctx = {
        'owner': instance.task.owner or instance.task.user,
        'applicant': instance.user,
        'accepted': instance.status == STATUS_ACCEPTED,
        'task': instance.task,
        'task_url': '%s/work/%s/' % (TUNGA_URL, instance.task.id)
    }
    send_mail(
        subject, 'tunga/email/email_task_application_response', to, ctx,
        **dict(deal_ids=[instance.task.hubspot_deal_id])
    )

@job
def notify_task_application_response_admin_email(instance):
    instance = clean_instance(instance, Application)
    subject = "Task application {}".format(instance.status == STATUS_ACCEPTED and 'accepted' or 'rejected')
    to = TUNGA_STAFF_UPDATE_EMAIL_RECIPIENTS
    ctx = {
        'owner': instance.task.owner or instance.task.user,
        'applicant': instance.user,
        'accepted': instance.status == STATUS_ACCEPTED,
        'task': instance.task,
        'task_url': '%s/work/%s/' % (TUNGA_URL, instance.task.id)
    }
    send_mail(
        subject, 'tunga/email/email_task_application_response', to, ctx,
        **dict(deal_ids=[instance.task.hubspot_deal_id])
    )


@job
def notify_task_application_response_slack(instance, admin=True):
    instance = clean_instance(instance, Application)

    application_url = '%s/work/%s/applications/' % (TUNGA_URL, instance.task_id)
    task_url = '%s/work/%s/' % (TUNGA_URL, instance.task.id)
    slack_msg = "Task Application {} | <{}|View on Tunga>".format(
        instance.status == STATUS_ACCEPTED and 'accepted' or 'rejected',
        task_url
    )

    attachments = [
        {
            slack_utils.KEY_TITLE: instance.task.summary,
            slack_utils.KEY_TITLE_LINK: application_url,
            slack_utils.KEY_TEXT: '%s%s%s%s\n\n<%s|View on Tunga>' %
                                  (truncatewords(convert_to_text(instance.pitch), 100),
                                   instance.hours_needed and '\n*Workload:* {} hrs'.format(instance.hours_needed) or '',
                                   instance.deliver_at and '\n*Delivery Date:* {}'.format(
                                       instance.deliver_at.strftime("%d %b, %Y")
                                   ) or '',
                                   instance.remarks and '\n*Remarks:* {}'.format(
                                       truncatewords(convert_to_text(instance.remarks), 100)
                                   ) or '',
                                   application_url),
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_TUNGA
        }
    ]
    if admin:
        slack_utils.send_incoming_webhook(
            SLACK_STAFF_INCOMING_WEBHOOK,
            {
                slack_utils.KEY_TEXT: slack_msg,
                slack_utils.KEY_ATTACHMENTS: attachments,
                slack_utils.KEY_CHANNEL: SLACK_STAFF_UPDATES_CHANNEL
            }
        )
    else:
        slack_utils.send_integration_message(instance.task, message=slack_msg, attachments=attachments)


@job
def send_task_application_not_selected_email(instance):
    instance = clean_instance(instance, Task)
    rejected_applicants = instance.application_set.filter(
        status=STATUS_REJECTED
    )
    if rejected_applicants:
        subject = "Your application was not accepted for: {}".format(instance.summary)
        to = [rejected_applicants[0].user.email]
        bcc = [dev.user.email for dev in rejected_applicants[1:]] if len(rejected_applicants) > 1 else None
        ctx = {
            'task': instance,
            'task_url': '%s/work/%s/' % (TUNGA_URL, instance.id)
        }
        send_mail(
            subject, 'tunga/email/email_task_application_not_selected', to, ctx, bcc=bcc,
            **dict(deal_ids=[instance.hubspot_deal_id])
        )


@job
def remind_progress_event(instance):
    remind_progress_event_email(instance)
    remind_progress_event_slack(instance)


@job
def remind_progress_event_email(instance):
    instance = clean_instance(instance, ProgressEvent)

    is_pm_report = instance.type == PROGRESS_EVENT_TYPE_PM
    is_client_report = instance.type == PROGRESS_EVENT_TYPE_CLIENT
    is_pm_or_client_report = is_pm_report or is_client_report

    if is_pm_report and not instance.task.is_project:
        return

    pm = instance.task.pm
    if not pm and instance.task.user.is_project_manager:
        pm = instance.task.user

    if is_pm_report and not pm:
        return

    owner = instance.task.owner
    if not owner:
        owner = instance.task.user

    if is_client_report and not owner:
        return

    subject = is_client_report and "Weekly Survey" or "Upcoming {} Update".format(instance.task.is_task and 'Task' or 'Project')

    to = []
    bcc = None
    if is_pm_report:
        to = [pm.email]
        bcc = None
    elif is_client_report:
        to = [owner.email]
        if owner.email != instance.task.user.email:
            to.append(instance.task.user.email)
        bcc = None
    else:
        participants = instance.task.participation_set.filter(status=STATUS_ACCEPTED)
        if participants:
            to = [participants[0].user.email]
            bcc = [participant.user.email for participant in participants[1:]] if participants.count() > 1 else None
    ctx = {
        'owner': instance.task.owner or instance.task.user,
        'event': instance,
        'update_url': '%s/work/%s/event/%s/' % (TUNGA_URL, instance.task.id, instance.id)
        }

    if to:
        email_template = is_client_report and 'client_survey_reminder' or 'progress_event_reminder'
        if send_mail(
                subject, 'tunga/email/{}'.format(email_template), to, ctx, bcc=bcc,
                **dict(deal_ids=[instance.task.hubspot_deal_id])
        ):
            instance.last_reminder_at = datetime.datetime.utcnow()
            instance.save()

@job
def remind_progress_event_slack(instance):
    instance = clean_instance(instance, ProgressEvent)

    task_integration = get_task_integration(instance.task, APP_INTEGRATION_PROVIDER_SLACK)
    if not task_integration:
        return

    is_pm_report = instance.type == PROGRESS_EVENT_TYPE_PM
    is_client_report = instance.type == PROGRESS_EVENT_TYPE_CLIENT
    is_pm_or_client_report = is_pm_report or is_client_report
    is_dev_report = not is_pm_or_client_report

    bot_access_token = task_integration.bot_access_token
    if not bot_access_token:
        if is_pm_report or is_dev_report:
            pass
            # TODO: set bot token to Tunga developers slack team token
        return

    if is_pm_report and not instance.task.is_project:
        return

    pm = instance.task.pm
    if not pm and instance.task.user.is_project_manager:
        pm = instance.task.user

    if is_pm_report and not pm:
        return

    owner = instance.task.owner
    if not owner:
        owner = instance.task.user

    if is_client_report and not owner:
        return

    slack_msg = "{} for \"{}\" | <{}|{} on Tunga>".format(
        is_client_report and "Weekly Survey" or "Upcoming {} Update".format(
            instance.task.is_task and 'Task' or 'Project'
        ),
        instance.task.summary,
        '{}/work/{}/event/{}/'.format(TUNGA_URL, instance.task.id, instance.id),
        is_client_report and "Take the survey" or "Give the update"
    )

    to_emails = []
    if is_pm_report:
        to_emails = [pm.email]

    elif is_client_report:
        to_emails = [owner.email]
        if owner.email != instance.task.user.email:
            to_emails.append(instance.task.user.email)

    else:
        participants = instance.task.participation_set.filter(status=STATUS_ACCEPTED)
        if participants:
            for participant in participants:
                to_emails.append(participant.user.email)

    if to_emails:
        for email in to_emails:
            im_id = get_user_im_id(email, bot_access_token)
            if im_id:
                slack_utils.send_slack_message(bot_access_token, im_id, message=slack_msg)


@job
def notify_new_progress_report(instance):
    notify_new_progress_report_email(instance)
    notify_new_progress_report_slack(instance)


@job
def notify_new_progress_report_email(instance):
    instance = clean_instance(instance, ProgressReport)
    is_pm_report = instance.event.type == PROGRESS_EVENT_TYPE_PM
    is_client_report = instance.event.type == PROGRESS_EVENT_TYPE_CLIENT
    is_pm_or_client_report = is_pm_report or is_client_report

    subject = "{} submitted a {}".format(instance.user.display_name, is_client_report and "Weekly Survey" or "Progress Report")

    to = is_pm_or_client_report and TUNGA_STAFF_UPDATE_EMAIL_RECIPIENTS or [instance.event.task.user.email]
    if instance.event.task.owner and not is_pm_or_client_report:
        to.append(instance.event.task.owner.email)
    ctx = {
        'owner': instance.event.task.user,
        'reporter': instance.user,
        'event': instance.event,
        'report': instance,
        'update_url': '{}/work/{}/event/{}/'.format(TUNGA_URL, instance.event.task.id, instance.event.id)
    }

    email_template = is_client_report and 'new_client_survey' or 'new_progress_report{}'.format(is_pm_report and '_pm' or '')
    send_mail(
        subject, 'tunga/email/{}'.format(email_template), to, ctx,
        **dict(deal_ids=[instance.event.task.hubspot_deal_id])
    )


@job
def notify_parties_of_low_rating_email(instance):
    instance = clean_instance(instance, ProgressReport)
    is_client_report = instance.event.type == PROGRESS_EVENT_TYPE_CLIENT

    # if is_client_report and instance.event.rate_deliverables < 5:
    if is_client_report:
        subject = "Work Rating For {}".format(instance.event.task.title)
        ctx = {
            'owner': instance.task.owner or instance.task.user or instance.task.pm,
            'event': instance,
            'update_url': '%s/work/%s/event/%s/' % (TUNGA_URL, instance.task.id, instance.id)
        }
        # send to client
        if instance.task.owner:
            to = [instance.event.task.owner.email]
            email_template = 'notification_low_rating_client'
            send_mail(
                subject, 'tunga/email/{}'.format(email_template), to, ctx,
                **dict(deal_ids=[instance.event.task.hubspot_deal_id])
            )
        # send to pm
        if instance.task.pm:
            to = [instance.event.task.pm.email]
            email_template = 'notification_low_rating_pm'
            send_mail(
                subject, 'tunga/email/{}'.format(email_template), to, ctx,
                **dict(deal_ids=[instance.event.task.hubspot_deal_id])
            )
        # send to user
        if instance.task.user:
            to = [instance.event.task.user.email]
            email_template = 'notification_low_rating_user'
            send_mail(
                subject, 'tunga/email/{}'.format(email_template), to, ctx,
                **dict(deal_ids=[instance.event.task.hubspot_deal_id])
            )


@job
def notify_pm_dev_when_stuck_email(instance):
    instance = clean_instance(instance, ProgressReport)

    is_pm_report = instance.event.type == PROGRESS_EVENT_TYPE_PM
    is_client_report = instance.event.type == PROGRESS_EVENT_TYPE_CLIENT
    is_pm_or_client_report = is_pm_report or is_client_report
    is_dev_report = not is_pm_or_client_report

    subject = "{} Follow Up on {}".format(instance.task.user.short_name, instance.event.task.title)
    ctx = {
        'owner': instance.task.owner or instance.task.user or instance.task.pm,
        'event': instance,
        'update_url': '%s/work/%s/event/%s/' % (TUNGA_URL, instance.task.id, instance.id)
    }

    if is_pm_report:
            to = [instance.event.task.pm.email]
            email_template = 'follow_up_when_stuck_pm'
    else:
        if is_dev_report:
            to = [instance.event.task.user.email]
            email_template = 'follow_up_when_stuck_dev'

    send_mail(
        subject, 'tunga/email/{}'.format(email_template), to, ctx,
        **dict(deal_ids=[instance.event.task.hubspot_deal_id])
    )


@job
def notify_dev_pm_on_failure_to_meet_deadline(instance):
    instance = clean_instance(instance, ProgressReport)

    is_pm_report = instance.event.type == PROGRESS_EVENT_TYPE_PM
    is_client_report = instance.event.type == PROGRESS_EVENT_TYPE_CLIENT
    is_pm_or_client_report = is_pm_report or is_client_report
    is_dev_report = not is_pm_or_client_report

    subject = "{} Follow Up on {}".format(instance.task.user.short_name, instance.event.task.title)
    ctx = {
        'owner': instance.task.owner or instance.task.user or instance.task.pm,
        'event': instance,
        'update_url': '%s/work/%s/event/%s/' % (TUNGA_URL, instance.task.id, instance.id)
    }
    to = [instance.event.task.pm.email]

    if is_pm_report:
            email_template = 'follow_up_when_wont_meet_deadline_pm'
    else:
        if is_dev_report:
            to.append(instance.event.task.user.email)
            email_template = 'follow_up_when_wont_meet_deadline_dev'

    send_mail(
        subject, 'tunga/email/{}'.format(email_template), to, ctx,
        **dict(deal_ids=[instance.event.task.hubspot_deal_id])
    )


def create_progress_report_slack_message(instance, updated=False, to_client=False):
    is_pm_report = instance.event.type == PROGRESS_EVENT_TYPE_PM
    is_client_report = instance.event.type == PROGRESS_EVENT_TYPE_CLIENT
    is_pm_or_client_report = is_pm_report or is_client_report
    is_dev_report = not is_pm_or_client_report

    report_url = '%s/work/%s/event/%s/' % (TUNGA_URL, instance.event.task_id, instance.event_id)
    slack_msg = "{} {} a {} | {}".format(
        instance.user.display_name,
        updated and 'updated' or 'submitted',
        is_client_report and "Weekly Survey" or "Progress Report",
        '<{}|View on Tunga>'.format(report_url)
    )

    slack_text_suffix = ''
    if not is_client_report:
        slack_text_suffix += '*Status:* {}\n*Percentage completed:* {}{}'.format(
            instance.get_status_display(), instance.percentage, '%')
    if not to_client:
        if instance.last_deadline_met is not None:
            slack_text_suffix += '\n*Was the last deadline met?:* {}'.format(
                instance.last_deadline_met and 'Yes' or 'No'
            )
        if instance.next_deadline:
            slack_text_suffix += '\n*Next deadline:* {}'.format(instance.next_deadline.strftime("%d %b, %Y"))
    if is_client_report:
        if instance.deliverable_satisfaction is not None:
            slack_text_suffix += '\n*Are you satisfied with the deliverables?:* {}'.format(
                instance.deliverable_satisfaction and 'Yes' or 'No'
            )
    if is_dev_report:
        if instance.stuck_reason:
            slack_text_suffix += '\n*Reason for being stuck:*\n {}'.format(
                convert_to_text(instance.get_stuck_reason_display())
            )
    attachments = [
        {
            slack_utils.KEY_TITLE: instance.event.task.summary,
            slack_utils.KEY_TITLE_LINK: report_url,
            slack_utils.KEY_TEXT: slack_text_suffix,
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_BLUE
        }
    ]

    if not to_client:
        if instance.deadline_miss_communicated is not None:
            attachments.append({
                slack_utils.KEY_TITLE: '{} promptly about not making the deadline?'.format(
                    is_client_report and 'Did the project manager/ developer(s) inform you' or 'Did you inform the client'),
                slack_utils.KEY_TEXT: '{}'.format(instance.deadline_miss_communicated and 'Yes' or 'No'),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_RED
            })

    if instance.deadline_report:
        attachments.append({
            slack_utils.KEY_TITLE: 'Report about the last deadline:',
            slack_utils.KEY_TEXT: convert_to_text(instance.deadline_report),
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_RED
        })

    if is_client_report:
        if instance.rate_deliverables:
            attachments.append({
                slack_utils.KEY_TITLE: 'How would you rate the deliverables on a scale from 1 to 5?',
                slack_utils.KEY_TEXT: '{}/5'.format(instance.rate_deliverables),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_BLUE
            })
        if instance.pm_communication:
            attachments.append({
                slack_utils.KEY_TITLE: 'Is the communication between you and the project manager/developer(s) going well?',
                slack_utils.KEY_TEXT: '{}'.format(instance.pm_communication and 'Yes' or 'No'),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_GREEN
            })
    else:
        # Status
        if instance.stuck_details:
            attachments.append({
                slack_utils.KEY_TITLE: 'Explain Further why you are stuck/what should be done:',
                slack_utils.KEY_TEXT: convert_to_text(instance.stuck_details),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_GREEN
            })

        if instance.started_at and not to_client:
            attachments.append({
                slack_utils.KEY_TITLE: 'When did you start this sprint/task/project?',
                slack_utils.KEY_TEXT: instance.started_at.strftime("%d %b, %Y"),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_BLUE
            })

        # Last
        if instance.accomplished:
            attachments.append({
                slack_utils.KEY_TITLE: 'What has been accomplished since last update?',
                slack_utils.KEY_TEXT: convert_to_text(instance.accomplished),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_GREEN
            })
        if instance.rate_deliverables and not to_client:
            attachments.append({
                slack_utils.KEY_TITLE: 'Rate Deliverables:',
                slack_utils.KEY_TEXT: '{}/5'.format(instance.rate_deliverables),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_RED
            })

        # Current
        if instance.todo:
            attachments.append({
                slack_utils.KEY_TITLE: is_dev_report and 'What do you intend to achieve/complete today?' or 'What are the next next steps?',
                slack_utils.KEY_TEXT: convert_to_text(instance.todo),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_GREEN
            })

        if not to_client:
            # Next
            if instance.next_deadline:
                attachments.append({
                    slack_utils.KEY_TITLE: 'When is the next deadline?',
                    slack_utils.KEY_TEXT: instance.next_deadline.strftime("%d %b, %Y"),
                    slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                    slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_RED
                })

            # Keep information about failures to meet deadlines internal
            if instance.next_deadline_meet is not None:
                attachments.append({
                    slack_utils.KEY_TITLE: 'Do you anticipate to meet this deadline?',
                    slack_utils.KEY_TEXT: '{}'.format(instance.next_deadline_meet and 'Yes' or 'No'),
                    slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                    slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_GREEN
                })
            if instance.next_deadline_fail_reason:
                attachments.append({
                    slack_utils.KEY_TITLE: 'Why will you not be able to make the next deadline?',
                    slack_utils.KEY_TEXT: convert_to_text(instance.next_deadline_fail_reason),
                    slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                    slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_RED
                })
        if instance.obstacles:
            attachments.append({
                slack_utils.KEY_TITLE: 'What obstacles are impeding your progress?',
                slack_utils.KEY_TEXT: convert_to_text(instance.obstacles),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_RED
            })

    if is_pm_report:
        if instance.team_appraisal:
            attachments.append({
                slack_utils.KEY_TITLE: 'Team appraisal:',
                slack_utils.KEY_TEXT: convert_to_text(instance.team_appraisal),
                slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
                slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_NEUTRAL
            })

    if instance.remarks:
        attachments.append({
            slack_utils.KEY_TITLE: 'Other remarks or questions',
            slack_utils.KEY_TEXT: convert_to_text(instance.remarks),
            slack_utils.KEY_MRKDWN_IN: [slack_utils.KEY_TEXT],
            slack_utils.KEY_COLOR: SLACK_ATTACHMENT_COLOR_NEUTRAL
        })

    return slack_msg, attachments


@job
def notify_new_progress_report_slack(instance, updated=False):
    instance = clean_instance(instance, ProgressReport)

    is_pm_report = instance.event.type == PROGRESS_EVENT_TYPE_PM
    is_client_report = instance.event.type == PROGRESS_EVENT_TYPE_CLIENT
    is_pm_or_client_report = is_pm_report or is_client_report
    is_dev_report = not is_pm_or_client_report

    #if not (slack_utils.is_task_notification_enabled(instance.event.task, slugs.EVENT_PROGRESS)):
    #    return

    # All reports go to Tunga #updates Slack
    slack_msg, attachments = create_progress_report_slack_message(instance, updated=updated)
    slack_utils.send_incoming_webhook(SLACK_STAFF_INCOMING_WEBHOOK, {
        slack_utils.KEY_TEXT: slack_msg,
        slack_utils.KEY_CHANNEL: SLACK_STAFF_UPDATES_CHANNEL,
        slack_utils.KEY_ATTACHMENTS: attachments
    })
    if is_dev_report:
        # Re-create report for clients
        slack_msg, attachments = create_progress_report_slack_message(instance, updated=updated, to_client=True)
        slack_utils.send_integration_message(instance.event.task, message=slack_msg, attachments=attachments)


@job
def notify_task_invoice_request_email(instance):
    instance = clean_instance(instance, Task)
    subject = "{} requested for an invoice".format(instance.user.display_name)
    to = TUNGA_STAFF_UPDATE_EMAIL_RECIPIENTS
    ctx = {
        'owner': instance.owner or instance.user,
        'task': instance,
        'task_url': '%s/work/%s/' % (TUNGA_URL, instance.id),
        'invoice_url': '%s/api/task/%s/download/invoice/?format=pdf' % (TUNGA_URL, instance.id)
    }
    send_mail(
        subject, 'tunga/email/email_task_invoice_request', to, ctx, **dict(deal_ids=[instance.hubspot_deal_id])
    )


@job
def notify_to_client_missed_and_communicated(instance):
    instance = clean_instance(instance, ProgressReport)
    to = is_pm_or_client_report and TUNGA_STAFF_UPDATE_EMAIL_RECIPIENTS or [
        instance.event.task.user.email
    ]

    if instance.event.task.owner and not is_pm_or_client_report:
        to.append(instance.event.task.owner.email)

    subject = "{} status update.".format(instance.task_id)
    ctx = {
        'owner': instance.owner or instance.user,
        'task': instance,
    }
    send_mail(
        subject, 'tunga/email/email_task_to_client_missed_and_communicated',
        to, ctx, **dict(deal_ids=[instance.hubspot_deal_id])
    )


@job
def notify_to_client_missed_and_not_communicated(instance):
    pass


@job
def notify_to_pm_missed_and_communicated(instance):
    pass


@job
def notify_to_pm_missed_and_not_communicated(instance):
    pass


@job
def notify_to_dev_progress_report(instance):
    pass


@job
def notify_to_client_not_satisfied_with_deliverables(instance):
    pass


@job
def notify_to_dev_and_pm_not_satisfied_with_deliverables(instance):
    pass