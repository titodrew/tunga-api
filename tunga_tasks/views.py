import datetime
from decimal import Decimal
from urllib import urlencode, quote_plus

from allauth.socialaccount.providers.github.provider import GitHubProvider
from dateutil.parser import parse
from django.contrib.contenttypes.models import ContentType
from django.http.response import HttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils.crypto import get_random_string
from django.views.decorators.csrf import csrf_exempt
from dry_rest_permissions.generics import DRYPermissions, DRYObjectPermissions
from oauthlib import oauth1
from oauthlib.oauth1 import SIGNATURE_TYPE_QUERY
from rest_framework import viewsets, status
from rest_framework.decorators import detail_route, api_view, permission_classes
from rest_framework.exceptions import ValidationError, NotAuthenticated, PermissionDenied
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated, AllowAny, IsAdminUser
from rest_framework.renderers import StaticHTMLRenderer
from rest_framework.response import Response
from rest_framework.reverse import reverse
from stripe.error import InvalidRequestError
from weasyprint import HTML

from tunga.settings import BITONIC_CONSUMER_KEY, BITONIC_CONSUMER_SECRET, BITONIC_ACCESS_TOKEN, BITONIC_TOKEN_SECRET, \
    BITONIC_URL, BITONIC_PAYMENT_COST_PERCENTAGE
from tunga_activity.filters import ActionFilter
from tunga_activity.models import ActivityReadLog
from tunga_activity.serializers import SimpleActivitySerializer, LastReadActivitySerializer
from tunga_profiles.models import DeveloperNumber
from tunga_tasks import slugs
from tunga_tasks.filterbackends import TaskFilterBackend, ApplicationFilterBackend, ParticipationFilterBackend, \
    TimeEntryFilterBackend, ProjectFilterBackend, ProgressReportFilterBackend, \
    ProgressEventFilterBackend
from tunga_tasks.filters import TaskFilter, ApplicationFilter, ParticipationFilter, TimeEntryFilter, \
    ProjectFilter, ProgressReportFilter, ProgressEventFilter, EstimateFilter, QuoteFilter, TaskPaymentFilter, \
    ParticipantPaymentFilter
from tunga_tasks.models import Task, Application, Participation, TimeEntry, Project, ProgressReport, ProgressEvent, \
    Integration, IntegrationMeta, IntegrationActivity, TaskPayment, TaskInvoice, Estimate, Quote, \
    MultiTaskPaymentKey, ParticipantPayment, SkillsApproval
from tunga_tasks.notifications import notify_task_invoice_request_email
from tunga_tasks.renderers import PDFRenderer
from tunga_tasks.serializers import TaskSerializer, ApplicationSerializer, ParticipationSerializer, \
    TimeEntrySerializer, ProjectSerializer, ProgressReportSerializer, ProgressEventSerializer, \
    IntegrationSerializer, TaskPaySerializer, TaskInvoiceSerializer, EstimateSerializer, QuoteSerializer, \
    MultiTaskPaymentKeySerializer, TaskPaymentSerializer, ParticipantPaymentSerializer, SkillsApprovalSerializer
from tunga_tasks.tasks import distribute_task_payment, generate_invoice_number, complete_bitpesa_payment
from tunga_tasks.utils import save_integration_tokens, get_integration_token
from tunga_utils import github, coinbase_utils, bitcoin_utils, bitpesa, stripe_utils
from tunga_utils.constants import TASK_PAYMENT_METHOD_BITONIC, TASK_PAYMENT_METHOD_BANK, STATUS_ACCEPTED, \
    TASK_PAYMENT_METHOD_STRIPE, CURRENCY_EUR, TASK_PAYMENT_METHOD_BITCOIN
from tunga_utils.filterbackends import DEFAULT_FILTER_BACKENDS
from tunga_utils.mixins import SaveUploadsMixin
from tunga_utils.serializers import InvoiceUserSerializer


class ProjectViewSet(viewsets.ModelViewSet):
    """
    Project Resource
    ---
    list:
        parameters_strategy: merge
        parameters:
            - name: filter
              description: Project filter e.g [running]
              type: string
              paramType: query
    """
    queryset = Project.objects.exclude(archived=True)
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated, DRYPermissions]
    filter_class = ProjectFilter
    filter_backends = DEFAULT_FILTER_BACKENDS + (ProjectFilterBackend,)
    search_fields = ('title', 'description')

    def perform_destroy(self, instance):
        instance.archived = True
        instance.archived_at = datetime.datetime.utcnow()
        instance.save()


class TaskViewSet(viewsets.ModelViewSet, SaveUploadsMixin):
    """
    Task Resource
    ---
    list:
        parameters_strategy: merge
        parameters:
            - name: filter
              description: Task filter e.g [running, my-tasks, saved, skills, my-clients]
              type: string
              paramType: query
    """
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [DRYPermissions]
    filter_class = TaskFilter
    filter_backends = DEFAULT_FILTER_BACKENDS + (TaskFilterBackend,)
    search_fields = ('title', 'description', 'skills__name')

    def perform_destroy(self, instance):
        instance.archived = True
        instance.archived_at = datetime.datetime.utcnow()
        instance.save()

    @detail_route(
        methods=['post'], url_path='read',
        permission_classes=[IsAuthenticated], serializer_class=LastReadActivitySerializer
    )
    def update_read(self, request, pk=None):
        """
        Updates user's read_at for channel
        ---
        request_serializer: LastReadActivitySerializer
        response_serializer: TaskSerializer
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        last_read = serializer.validated_data['last_read']
        task = get_object_or_404(self.get_queryset(), pk=pk)
        if task.has_object_read_permission(request):
            ActivityReadLog.objects.update_or_create(
                user=request.user,
                content_type=ContentType.objects.get_for_model(task), object_id=task.id,
                defaults={'last_read': last_read}
            )
            response_serializer = TaskSerializer(task, context={'request': request})
            return Response(response_serializer.data)
        return Response(
            {'status': 'Unauthorized', 'message': 'No access to this task'},
            status=status.HTTP_401_UNAUTHORIZED
        )

    @detail_route(
        methods=['post'], url_path='claim',
        permission_classes=[IsAuthenticated]
    )
    def claim(self, request, pk=None):
        """
        Claim a project
        ---
        response_serializer: TaskSerializer
        """
        task = get_object_or_404(self.get_queryset(), pk=pk)
        if task.has_object_read_permission(request):
            task.pm = request.user
            task.save()
            response_serializer = TaskSerializer(task, context={'request': request})
            return Response(response_serializer.data)
        return Response(
            {'status': 'Unauthorized', 'message': 'No access to this task'},
            status=status.HTTP_401_UNAUTHORIZED
        )

    @detail_route(
        methods=['post'], url_path='return',
        permission_classes=[IsAuthenticated]
    )
    def return_project(self, request, pk=None):
        """
        Return a project
        ---
        request_serializer: None
        response_serializer: TaskSerializer
        """
        task = get_object_or_404(self.get_queryset(), pk=pk)
        if task.has_object_read_permission(request):
            task.pm = None
            task.save()
            response_serializer = TaskSerializer(task, context={'request': request})
            return Response(response_serializer.data)
        return Response(
            {'status': 'Unauthorized', 'message': 'No access to this task'},
            status=status.HTTP_401_UNAUTHORIZED
        )

    @detail_route(
        methods=['get'], url_path='activity',
        permission_classes=[IsAuthenticated],
        serializer_class=SimpleActivitySerializer,
        filter_class=None,
        filter_backends=DEFAULT_FILTER_BACKENDS,
        search_fields=('comments__body',)
    )
    def activity(self, request, pk=None):
        """
        Task Activity Endpoint
        ---
        response_serializer: SimpleActivitySerializer
        omit_parameters:
            - query
        """
        task = get_object_or_404(self.get_queryset(), pk=pk)
        self.check_object_permissions(request, task)

        queryset = ActionFilter(request.GET, self.filter_queryset(task.activity_stream.all().order_by('-id')))
        page = self.paginate_queryset(queryset.qs)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @detail_route(
        methods=['get', 'post', 'put'], url_path='invoice',
        serializer_class=TaskPaySerializer, permission_classes=[IsAuthenticated]
    )
    def invoice(self, request, pk=None):
        """
        Task Invoice Endpoint
        ---
        request_serializer: TaskPaySerializer
        response_serializer: TaskInvoiceSerializer
        omit_parameters:
            - query
        """
        task = get_object_or_404(self.get_queryset(), pk=pk)
        self.check_object_permissions(request, task)

        invoice = task.invoice

        if request.method == 'POST':
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            fee = serializer.validated_data['fee']
            payment_method = serializer.validated_data['payment_method']
            withhold_tunga_fee = serializer.validated_data['withhold_tunga_fee']

            if fee < task.pay:
                raise ValidationError({
                    'fee': 'You cannot reduce the fee for the task, Please contact support@tunga.io for assistance'
                })

            task.bid = fee
            task.payment_method = payment_method
            task.withhold_tunga_fee = withhold_tunga_fee

            btc_price = coinbase_utils.get_btc_price(task.currency)
            task.btc_price = btc_price

            if not task.btc_address or not bitcoin_utils.is_valid_btc_address(task.btc_address):
                address = coinbase_utils.get_new_address(coinbase_utils.get_api_client())
                task.btc_address = address

            task.full_clean()
            task.save()

            developer = None
            try:
                assignee = task.participation_set.filter(
                    status=STATUS_ACCEPTED
                ).order_by('-assignee').earliest('created_at')
                developer = assignee.user
            except:
                pass

            if not developer:
                raise ValidationError({
                    'fee': 'Please assign a developer to the task or contact support@tunga.io for assistance'
                })

            # Save Invoice
            invoice = TaskInvoice.objects.create(
                task=task,
                user=request.user,
                title=task.title,
                fee=task.pay,
                client=task.owner or task.user,
                developer=developer,
                payment_method=task.payment_method,
                btc_price=btc_price,
                btc_address=task.btc_address,
                withhold_tunga_fee=task.withhold_tunga_fee
            )

            if task.payment_method == TASK_PAYMENT_METHOD_BANK:
                # Send notification for requested invoice
                notify_task_invoice_request_email.delay(task.id)

        response_serializer = TaskInvoiceSerializer(invoice, context={'request': request})
        return Response(response_serializer.data)

    @detail_route(
        methods=['get', 'post'], url_path='pay/(?P<provider>[^/]+)',
        serializer_class=TaskPaySerializer,
        permission_classes=[IsAuthenticated]
    )
    def pay(self, request, pk=None, provider=None):
        """
            Task Payment Provider Endpoint
            ---
            omit_serializer: true
            omit_parameters:
                - query
            """
        task = self.get_object()

        if provider == TASK_PAYMENT_METHOD_STRIPE:
            # Pay with Stripe
            payload = request.data
            paid_at = datetime.datetime.utcnow()

            stripe = stripe_utils.get_client()

            try:
                customer = stripe.Customer.create(**dict(source=payload['token'], email=payload['email']))

                charge = stripe.Charge.create(
                    idempotency_key=payload.get('idem_key', None),
                    **dict(
                        amount=payload['amount'],
                        description=payload.get('description', task.summary),
                        currency=payload.get('currency', CURRENCY_EUR),
                        customer=customer.id,
                        metadata=dict(
                            task_id=task.id,
                            invoice_id=payload.get('invoice_id', '')
                        )
                    )
                )

                task_pay, created = TaskPayment.objects.get_or_create(
                    task=task, ref=charge.id, payment_type=TASK_PAYMENT_METHOD_STRIPE,
                    defaults=dict(
                        token=payload['token'],
                        email=payload['email'],
                        amount=Decimal(charge.amount)*Decimal(0.01),
                        currency=(charge.currency or CURRENCY_EUR).upper(),
                        charge_id=charge.id,
                        paid=charge.paid,
                        captured=charge.captured,
                        received_at=paid_at
                    )
                )
                task.paid = True
                task.paid_at = paid_at
                task.save()

                # distribute_task_payment.delay(task.id)

                task_serializer = TaskSerializer(task, context={'request': request})
                task_payment_serializer = TaskPaymentSerializer(task_pay, context={'request': request})
                return Response(dict(task=task_serializer.data, payment=task_payment_serializer.data))
            except InvalidRequestError:
                return Response(dict(message='We could not process your payment! Please contact hello@tunga.io'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        elif provider == TASK_PAYMENT_METHOD_BITONIC:
            # Pay with Bitonic
            callback = '%s://%s/task/%s/rate/' % (request.scheme, request.get_host(), pk)
            next_url = callback
            if task and task.has_object_write_permission(request) and bitcoin_utils.is_valid_btc_address(task.btc_address):
                if provider == TASK_PAYMENT_METHOD_BITONIC:
                    client = oauth1.Client(
                        BITONIC_CONSUMER_KEY, BITONIC_CONSUMER_SECRET, BITONIC_ACCESS_TOKEN, BITONIC_TOKEN_SECRET,
                        callback_uri=callback, signature_type=SIGNATURE_TYPE_QUERY
                    )
                    amount = task.pay * (task.withhold_tunga_fee and (1 - task.tunga_ratio_dev) or 1)
                    increase_factor = 1 + (Decimal(BITONIC_PAYMENT_COST_PERCENTAGE)*Decimal(0.01))
                    q_string = urlencode({
                        'ext_data': task.summary.encode('utf-8'),
                        'bitcoinaddress': task.btc_address,
                        'ordertype': 'buy',
                        'euros': amount * increase_factor
                    })
                    req_data = client.sign('%s/?%s' % (BITONIC_URL, q_string), http_method='GET')
                    next_url = req_data[0]
            return redirect(next_url)

    @detail_route(
        methods=['get'], url_path='download/invoice',
        renderer_classes=[PDFRenderer, StaticHTMLRenderer],
        permission_classes=[AllowAny]
    )
    def download_invoice(self, request, pk=None):
        """
        Download Task Invoice Endpoint
        ---
        omit_serializer: True
        omit_parameters:
            - query
        """
        current_url = '%s?%s' % (
            reverse(request.resolver_match.url_name, kwargs={'pk': pk}),
            urlencode(request.query_params)
        )
        login_url = '/signin?next=%s' % quote_plus(current_url)
        if not request.user.is_authenticated():
            return redirect(login_url)
        task = get_object_or_404(self.get_queryset(), pk=pk)
        try:
            self.check_object_permissions(request, task)
        except NotAuthenticated:
            return redirect(login_url)
        except PermissionDenied:
            return HttpResponse("You do not have permission to access this invoice")

        invoice = task.invoice
        if invoice:
            if not invoice.number:
                try:
                    invoice = generate_invoice_number(invoice)
                except:
                    pass

            if invoice.number:
                invoice_data = TaskInvoiceSerializer(invoice).data

                context = {"is_developer": request.user.is_developer}
                if request.user.is_developer or request.user.is_staff:
                    invoice_type = request.query_params.get('type', None)
                    if invoice_type:
                        context["is_developer"] = invoice_type != 'client'
                invoice_data['date'] = task.invoice.created_at.strftime('%d %B %Y')

                task_developers = []
                for share_info in task.get_participation_shares():
                    participant = share_info['participant']
                    developer, created = DeveloperNumber.objects.get_or_create(user=participant.user)
                    task_developers.append({
                        'developer': InvoiceUserSerializer(participant.user).data,
                        'amount': invoice.get_amount_details(share=share_info['share']),
                        'number': invoice_data['number'] + developer.number + (context["is_developer"] and 'D' or 'C')
                    })

                invoice_data['developers'] = task_developers

                ctx = {
                    'user': request.user,
                    'context': context,
                    'invoice': invoice_data
                }

                rendered_html = render_to_string("tunga/pdf/invoice.html", context=ctx).encode(encoding="UTF-8")
                if request.accepted_renderer.format == 'html':
                    return HttpResponse(rendered_html)
                pdf_file = HTML(string=rendered_html, encoding='utf-8').write_pdf()
                http_response = HttpResponse(pdf_file, content_type='application/pdf')
                http_response['Content-Disposition'] = 'filename="invoice_%s.pdf"' % task.summary
                return http_response

        return HttpResponse("Could not generate an invoice, Please contact support@tunga.io")

    @detail_route(
        methods=['get'], url_path='download/estimate',
        renderer_classes=[PDFRenderer, StaticHTMLRenderer],
        permission_classes=[AllowAny]
    )
    def download_task_estimate(self, request, pk=None):
        """
        Download Task Estimate Endpoint
        ---
        omit_serializer: True
        omit_parameters:
            - query
        """
        current_url = '%s?%s' % (
            reverse(request.resolver_match.url_name, kwargs={'pk': pk}),
            urlencode(request.query_params)
        )
        login_url = '/signin?next=%s' % quote_plus(current_url)
        if not request.user.is_authenticated():
            return redirect(login_url)

        task = get_object_or_404(self.get_queryset(), pk=pk)

        estimate = task.estimate

        try:
            self.check_object_permissions(request, estimate)
        except NotAuthenticated:
            return redirect(login_url)
        except PermissionDenied:
            return HttpResponse("You do not have permission to access this estimate")

        if estimate:
            ctx = {
                'user': request.user,
                'estimate': estimate
            }

            rendered_html = render_to_string("tunga/pdf/estimate.html", context=ctx).encode(encoding="UTF-8")

            if request.accepted_renderer.format == 'html':
                return HttpResponse(rendered_html)

            pdf_file = HTML(string=rendered_html, encoding='utf-8').write_pdf()
            http_response = HttpResponse(pdf_file, content_type='application/pdf')
            http_response['Content-Disposition'] = 'filename="estimate.pdf"'
            return http_response
        return HttpResponse("Could not generate the estimate, Please contact support@tunga.io")

    @detail_route(
        methods=['get'], url_path='download/quote',
        renderer_classes=[PDFRenderer, StaticHTMLRenderer],
        permission_classes=[AllowAny]
    )
    def download_task_quote(self, request, pk=None):
        """
        Download Task Estimate Endpoint
        ---
        omit_serializer: True
        omit_parameters:
            - query
        """
        current_url = '%s?%s' % (
            reverse(request.resolver_match.url_name, kwargs={'pk': pk}),
            urlencode(request.query_params)
        )
        login_url = '/signin?next=%s' % quote_plus(current_url)
        if not request.user.is_authenticated():
            return redirect(login_url)

        task = get_object_or_404(self.get_queryset(), pk=pk)

        quote = task.quote

        try:
            self.check_object_permissions(request, quote)
        except NotAuthenticated:
            return redirect(login_url)
        except PermissionDenied:
            return HttpResponse("You do not have permission to access this quote")

        if quote:
            ctx = {
                'user': request.user,
                'quote': quote
            }

            rendered_html = render_to_string("tunga/pdf/quote.html", context=ctx).encode(encoding="UTF-8")

            if request.accepted_renderer.format == 'html':
                return HttpResponse(rendered_html)
            pdf_file = HTML(string=rendered_html, encoding='utf-8').write_pdf()
            http_response = HttpResponse(pdf_file, content_type='application/pdf')
            http_response['Content-Disposition'] = 'filename="task_quote.pdf"'
            return http_response
        return HttpResponse("Could not generate the quote, Please contact support@tunga.io")

    @detail_route(
        methods=['get', 'post', 'put', 'patch'], url_path='integration/(?P<provider>[^/]+)',
        serializer_class=IntegrationSerializer
    )
    def integration(self, request, pk=None, provider=None):
        """
        Manage Task Integrations
        ---
        serializer: IntegrationSerializer
        omit_parameters:
            - query
        """
        get_object_or_404(self.queryset, pk=pk)
        queryset = Integration.objects.filter(task_id=pk, provider=provider)
        if request.method == 'GET':
            instance = get_object_or_404(queryset)
            self.check_object_permissions(request, instance)
            serializer = self.get_serializer(instance, context={'request': request})
            return Response(serializer.data)
        elif request.method == 'POST':
            request_data = dict(request.data)
            request_data['provider'] = provider
            request_data['task'] = pk

            try:
                instance = queryset.latest('created_at')
            except Integration.DoesNotExist:
                instance = None

            if instance:
                self.check_object_permissions(request, instance)
            else:
                self.check_permissions(request)
            serializer = self.get_serializer(instance, data=request_data, context={'request': request})
            serializer.is_valid(raise_exception=True)

            if provider == GitHubProvider.id:
                secret = get_random_string()
                if instance:
                    secret = instance.secret or secret

                data = {
                    'name': 'web',
                    'config': {
                        'url': '%s://%s/task/%s/hook/%s/' % (request.scheme, request.get_host(), pk, provider),
                        'content_type': 'json',
                        'secret': secret
                    },
                    'events': github.transform_to_github_events(request_data['events']),
                    'active': True
                }

                repo_full_name = None
                repo = request_data.get('repo', None)
                if repo:
                    repo_full_name = repo.get('full_name', None)
                if not repo_full_name and instance:
                    repo_full_name = instance.repo_full_name

                if not repo_full_name:
                    return Response({'status': 'Bad Request'}, status.HTTP_400_BAD_REQUEST)

                web_hook_endpoint = '/repos/%s/hooks' % repo_full_name
                hook_method = 'post'

                if instance and instance.hook_id:
                    web_hook_endpoint += '/%s' % instance.hook_id
                    hook_method = 'patch'

                social_token = get_integration_token(request.user, provider, task=pk)
                if not social_token:
                    return Response({'status': 'Unauthorized'}, status.HTTP_401_UNAUTHORIZED)

                r = github.api(endpoint=web_hook_endpoint, method=hook_method, data=data, access_token=social_token.token)
                if r.status_code in [200, 201]:
                    hook = r.json()
                    integration = serializer.save(secret=secret)
                    if 'id' in hook:
                        IntegrationMeta.objects.update_or_create(
                                integration=integration, meta_key='hook_id', defaults={'meta_value': hook['id']}
                        )
                    if not integration.token:
                        save_integration_tokens(request.user, pk, provider)
                    return Response(serializer.data)
                return Response(r.json(), r.status_code)
            else:
                integration = serializer.save()
                if not integration.token:
                    save_integration_tokens(request.user, pk, provider)
                return Response(serializer.data)
        else:
            return Response({'status': 'Method not allowed'}, status.HTTP_405_METHOD_NOT_ALLOWED)

    @detail_route(
        methods=['post'], url_path='hook/(?P<provider>[^/]+)',
        permission_classes=[AllowAny]
    )
    def hook(self, request, pk=None, provider=None):
        """
        Task Integration Hook
        Receives web hooks from different providers
        ---
        omit_serializer: true
        omit_parameters:
            - query
        """
        try:
            integration = Integration.objects.filter(task_id=pk, provider=provider).latest('created_at')
        except:
            integration = None
        if integration:
            github_event_name = request.META.get(github.HEADER_EVENT_NAME, None)
            delivery_id = request.META.get(github.HEADER_DELIVERY_ID, None)
            activity = {}
            if github_event_name:
                payload = request.data

                if github_event_name == github.EVENT_PUSH:
                    # Push event
                    if payload[github.PAYLOAD_HEAD_COMMIT]:
                        head_commit = payload[github.PAYLOAD_HEAD_COMMIT]
                        activity[slugs.ACTIVITY_URL] = head_commit[github.PAYLOAD_URL]
                        activity[slugs.ACTIVITY_REF] = head_commit[github.PAYLOAD_ID]
                        activity[slugs.ACTIVITY_REF_NAME] = head_commit[github.PAYLOAD_TREE_ID]
                        activity[slugs.ACTIVITY_USERNAME] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_USERNAME]
                        activity[slugs.ACTIVITY_AVATAR_URL] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_AVATAR_URL]
                        activity[slugs.ACTIVITY_BODY] = head_commit[github.PAYLOAD_MESSAGE]
                        activity[slugs.ACTIVITY_CREATED_AT] = parse(head_commit[github.PAYLOAD_TIMESTAMP])
                elif github_event_name == github.EVENT_ISSUE:
                    # Issue
                    issue_actions = [
                        github.PAYLOAD_ACTION_OPENED, github.PAYLOAD_ACTION_CLOSED,
                        github.PAYLOAD_ACTION_EDITED, github.PAYLOAD_ACTION_REOPENED
                    ]
                    if payload[github.PAYLOAD_ISSUE] and payload[github.PAYLOAD_ACTION] in issue_actions:
                        issue = payload[github.PAYLOAD_ISSUE]
                        activity[slugs.ACTIVITY_ACTION] = payload[github.PAYLOAD_ACTION]
                        activity[slugs.ACTIVITY_URL] = issue[github.PAYLOAD_HTML_URL]
                        activity[slugs.ACTIVITY_REF] = issue[github.PAYLOAD_ID]
                        activity[slugs.ACTIVITY_REF_NAME] = issue[github.PAYLOAD_NUMBER]
                        activity[slugs.ACTIVITY_USERNAME] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_USERNAME]
                        activity[slugs.ACTIVITY_AVATAR_URL] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_AVATAR_URL]
                        activity[slugs.ACTIVITY_TITLE] = issue[github.PAYLOAD_TITLE]
                        activity[slugs.ACTIVITY_BODY] = issue[github.PAYLOAD_BODY]
                        activity[slugs.ACTIVITY_CREATED_AT] = parse(issue[github.PAYLOAD_CREATED_AT])
                elif github_event_name == github.EVENT_PULL_REQUEST:
                    # Pull Request
                    pull_request_actions = [
                        github.PAYLOAD_ACTION_OPENED, github.PAYLOAD_ACTION_CLOSED,
                        github.PAYLOAD_ACTION_EDITED, github.PAYLOAD_ACTION_REOPENED
                    ]
                    if payload[github.PAYLOAD_PULL_REQUEST] and payload[github.PAYLOAD_ACTION] in pull_request_actions:
                        pull_request = payload[github.PAYLOAD_PULL_REQUEST]
                        is_merged = payload[github.PAYLOAD_ACTION] == github.PAYLOAD_ACTION_CLOSED and pull_request[github.PAYLOAD_MERGED]
                        activity[slugs.ACTIVITY_ACTION] = is_merged and slugs.ACTION_MERGED or payload[github.PAYLOAD_ACTION]
                        activity[slugs.ACTIVITY_URL] = pull_request[github.PAYLOAD_HTML_URL]
                        activity[slugs.ACTIVITY_REF] = pull_request[github.PAYLOAD_ID]
                        activity[slugs.ACTIVITY_REF_NAME] = pull_request[github.PAYLOAD_NUMBER]
                        activity[slugs.ACTIVITY_USERNAME] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_USERNAME]
                        activity[slugs.ACTIVITY_AVATAR_URL] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_AVATAR_URL]
                        activity[slugs.ACTIVITY_TITLE] = pull_request[github.PAYLOAD_TITLE]
                        activity[slugs.ACTIVITY_BODY] = pull_request[github.PAYLOAD_BODY]
                        activity[slugs.ACTIVITY_CREATED_AT] = parse(pull_request[github.PAYLOAD_CREATED_AT])
                elif github_event_name in [github.EVENT_CREATE, github.EVENT_DELETE]:
                    # Branch and Tag creation and deletion
                    tracked_ref_types = [github.PAYLOAD_REF_TYPE_BRANCH, github.PAYLOAD_REF_TYPE_TAG]
                    if payload[github.PAYLOAD_REF_TYPE] in tracked_ref_types:
                        activity[slugs.ACTIVITY_EVENT_ID] = payload[github.PAYLOAD_REF_TYPE] == github.PAYLOAD_REF_TYPE_BRANCH and slugs.EVENT_BRANCH or slugs.EVENT_TAG
                        activity[slugs.ACTIVITY_ACTION] = github_event_name == github.EVENT_CREATE and slugs.ACTION_CREATED or slugs.ACTION_DELETED
                        activity[slugs.ACTIVITY_URL] = '%s/tree/%s' % (
                            payload[github.PAYLOAD_REPOSITORY][github.PAYLOAD_HTML_URL], payload[github.PAYLOAD_REF]
                        )
                        activity[slugs.ACTIVITY_REF] = payload[github.PAYLOAD_REF]
                        activity[slugs.ACTIVITY_USERNAME] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_USERNAME]
                        activity[slugs.ACTIVITY_AVATAR_URL] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_AVATAR_URL]
                elif github_event_name in [github.EVENT_COMMIT_COMMENT, github.EVENT_ISSUE_COMMENT, github.EVENT_PULL_REQUEST_REVIEW_COMMENT]:
                    # Commit, Issue and Pull Request comments
                    if payload[github.PAYLOAD_ACTION] == github.PAYLOAD_ACTION_CREATED:
                        comment = payload[github.PAYLOAD_COMMENT]
                        activity[slugs.ACTIVITY_ACTION] = slugs.ACTION_CREATED
                        activity[slugs.ACTIVITY_URL] = comment[github.PAYLOAD_HTML_URL]
                        activity[slugs.ACTIVITY_REF] = comment[github.PAYLOAD_ID]
                        activity[slugs.ACTIVITY_USERNAME] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_USERNAME]
                        activity[slugs.ACTIVITY_AVATAR_URL] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_AVATAR_URL]
                        activity[slugs.ACTIVITY_BODY] = comment[github.PAYLOAD_BODY]
                        activity[slugs.ACTIVITY_CREATED_AT] = parse(comment[github.PAYLOAD_CREATED_AT])
                elif github_event_name == github.EVENT_RELEASE:
                    # Release
                    release_actions = [github.PAYLOAD_ACTION_PUBLISHED]
                    if payload[github.PAYLOAD_RELEASE] and payload[github.PAYLOAD_ACTION] in release_actions:
                        release = payload[github.PAYLOAD_RELEASE]
                        activity[slugs.ACTIVITY_ACTION] = payload[github.PAYLOAD_ACTION]
                        activity[slugs.ACTIVITY_URL] = release[github.PAYLOAD_HTML_URL]
                        activity[slugs.ACTIVITY_REF] = release[github.PAYLOAD_ID]
                        activity[slugs.ACTIVITY_REF_NAME] = release[github.PAYLOAD_TAG_NAME]
                        activity[slugs.ACTIVITY_USERNAME] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_USERNAME]
                        activity[slugs.ACTIVITY_AVATAR_URL] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_AVATAR_URL]
                        activity[slugs.ACTIVITY_TITLE] = release[github.PAYLOAD_TITLE]
                        activity[slugs.ACTIVITY_BODY] = release[github.PAYLOAD_BODY]
                        activity[slugs.ACTIVITY_CREATED_AT] = parse(release[github.PAYLOAD_CREATED_AT])
                elif github_event_name == github.EVENT_GOLLUM:
                    # Wiki creation and updates
                    if payload[github.PAYLOAD_PAGES]:
                        first_page = payload[github.PAYLOAD_PAGES][0]
                        activity[slugs.ACTIVITY_ACTION] = first_page[github.PAYLOAD_ACTION] == github.PAYLOAD_ACTION_CREATED and slugs.ACTION_CREATED or slugs.ACTION_EDITED
                        activity[slugs.ACTIVITY_URL] = first_page[github.PAYLOAD_HTML_URL]
                        activity[slugs.ACTIVITY_REF] = payload[github.PAYLOAD_PAGE_NAME]
                        activity[slugs.ACTIVITY_USERNAME] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_USERNAME]
                        activity[slugs.ACTIVITY_AVATAR_URL] = payload[github.PAYLOAD_SENDER][github.PAYLOAD_AVATAR_URL]
                        activity[slugs.ACTIVITY_BODY] = first_page[github.PAYLOAD_SUMMARY]

                if activity:
                    if not activity.get(slugs.ACTIVITY_EVENT_ID, None):
                        activity[slugs.ACTIVITY_EVENT_ID] = github.transform_to_tunga_event(github_event_name)
                    activity[slugs.ACTIVITY_INTEGRATION] = integration
                    IntegrationActivity.objects.create(**activity)
        return Response({'status': 'Received'})


class ApplicationViewSet(viewsets.ModelViewSet):
    """
    Task Application Resource
    """
    queryset = Application.objects.all()
    serializer_class = ApplicationSerializer
    permission_classes = [IsAuthenticated, DRYPermissions]
    filter_class = ApplicationFilter
    filter_backends = DEFAULT_FILTER_BACKENDS + (ApplicationFilterBackend,)
    search_fields = ('task__title', 'task__skills__name', '^user__username', '^user__first_name', '^user__last_name')


class ParticipationViewSet(viewsets.ModelViewSet):
    """
    Task Participation Resource
    """
    queryset = Participation.objects.all()
    serializer_class = ParticipationSerializer
    permission_classes = [IsAuthenticated, DRYObjectPermissions]
    filter_class = ParticipationFilter
    filter_backends = DEFAULT_FILTER_BACKENDS + (ParticipationFilterBackend,)
    search_fields = ('task__title', 'task__skills__name', '^user__username', '^user__first_name', '^user__last_name')


class EstimateViewSet(viewsets.ModelViewSet):
    """
    Estimate Resource
    """
    queryset = Estimate.objects.all()
    serializer_class = EstimateSerializer
    permission_classes = [IsAuthenticated, DRYPermissions]
    filter_class = EstimateFilter
    #filter_backends = DEFAULT_FILTER_BACKENDS + (TimeEntryFilterBackend,)
    search_fields = ('title', 'introduction', '^task__title')

    @detail_route(
        methods=['get'], url_path='download',
        renderer_classes=[PDFRenderer, StaticHTMLRenderer],
        permission_classes=[AllowAny]
    )
    def download_estimate(self, request, pk=None):
        """
        Download Estimate Endpoint
        ---
        omit_serializer: True
        omit_parameters:
            - query
        """
        current_url = '%s?%s' % (
            reverse(request.resolver_match.url_name, kwargs={'pk': pk}),
            urlencode(request.query_params)
        )
        login_url = '/signin?next=%s' % quote_plus(current_url)
        if not request.user.is_authenticated():
            return redirect(login_url)

        estimate = get_object_or_404(self.get_queryset(), pk=pk)

        try:
            self.check_object_permissions(request, estimate)
        except NotAuthenticated:
            return redirect(login_url)
        except PermissionDenied:
            return HttpResponse("You do not have permission to access this estimate")

        if estimate:
            ctx = {
                'user': request.user,
                'estimate': estimate
            }

            rendered_html = render_to_string("tunga/pdf/estimate.html", context=ctx).encode(encoding="UTF-8")

            if request.accepted_renderer.format == 'html':
                return HttpResponse(rendered_html)

            pdf_file = HTML(string=rendered_html, encoding='utf-8').write_pdf()
            http_response = HttpResponse(pdf_file, content_type='application/pdf')
            http_response['Content-Disposition'] = 'filename="estimate.pdf"'
            return http_response
        return HttpResponse("Could not generate the estimate, Please contact support@tunga.io")


class QuoteViewSet(viewsets.ModelViewSet):
    """
    Quote Resource
    """
    queryset = Quote.objects.all()
    serializer_class = QuoteSerializer
    permission_classes = [IsAuthenticated, DRYPermissions]
    filter_class = QuoteFilter
    #filter_backends = DEFAULT_FILTER_BACKENDS + (TimeEntryFilterBackend,)
    search_fields = ('introduction', '^task__title')


class TimeEntryViewSet(viewsets.ModelViewSet):
    """
    Time Entry Resource
    """
    queryset = TimeEntry.objects.all()
    serializer_class = TimeEntrySerializer
    permission_classes = [IsAuthenticated, DRYPermissions]
    filter_class = TimeEntryFilter
    filter_backends = DEFAULT_FILTER_BACKENDS + (TimeEntryFilterBackend,)
    search_fields = ('description', '^task__title')


class ProgressEventViewSet(viewsets.ModelViewSet):
    """
    Progress Event Resource
    """
    queryset = ProgressEvent.objects.all()
    serializer_class = ProgressEventSerializer
    permission_classes = [IsAuthenticated, DRYPermissions]
    filter_class = ProgressEventFilter
    filter_backends = DEFAULT_FILTER_BACKENDS + (ProgressEventFilterBackend,)
    search_fields = (
        'title', 'description', '^task__title', '^task__skills__name',
        '^created_by__user__username', '^created_by__user__first_name', '^created_by__user__last_name',
    )


class ProgressReportViewSet(viewsets.ModelViewSet):
    """
    Progress Report Resource
    """
    queryset = ProgressReport.objects.all()
    serializer_class = ProgressReportSerializer
    permission_classes = [IsAuthenticated, DRYPermissions]
    filter_class = ProgressReportFilter
    filter_backends = DEFAULT_FILTER_BACKENDS + (ProgressReportFilterBackend,)
    search_fields = (
        '^user__username', '^user__first_name', '^user__last_name', 'accomplished', 'todo', 'remarks',
        'event__task__title', 'event__task__skills__name'
    )


class MultiTaskPaymentKeyViewSet(viewsets.ModelViewSet):
    """
    Multi Task Payments Resource
    """
    queryset = MultiTaskPaymentKey.objects.all()
    serializer_class = MultiTaskPaymentKeySerializer
    permission_classes = [IsAuthenticated]


class TaskPaymentViewSet(viewsets.ModelViewSet):
    """
    Task Payment Resource
    """
    queryset = TaskPayment.objects.all()
    serializer_class = TaskPaymentSerializer
    permission_classes = [IsAdminUser]
    filter_class = TaskPaymentFilter
    search_fields = (
        'task__title', '^task__user__username', '^task__user__first_name', '^task__user__last_name'
    )


class ParticipantPaymentViewSet(viewsets.ModelViewSet):
    """
    Participant Payment Resource
    """
    queryset = ParticipantPayment.objects.all()
    serializer_class = ParticipantPaymentSerializer
    permission_classes = [IsAdminUser]
    filter_class = ParticipantPaymentFilter
    search_fields = (
        'source__task__title', '^source__task__user__username',
        '^source__task__user__first_name', '^source__task__user__last_name'
    )






@csrf_exempt
@api_view(http_method_names=['POST'])
@permission_classes([AllowAny])
def coinbase_notification(request):
    client = coinbase_utils.get_api_client()

    # Verify that the request came from coinbase
    cb_signature = request.META.get(coinbase_utils.HEADER_COINBASE_SIGNATURE, None)
    if not client.verify_callback(request.body, cb_signature):
        return Response('Unauthorized Request', status=status.HTTP_401_UNAUTHORIZED)

    payload = request.data
    if payload.get(coinbase_utils.PAYLOAD_TYPE, None) == coinbase_utils.PAYLOAD_TYPE_NEW_PAYMENT:
        id = payload[coinbase_utils.PAYLOAD_ID]
        address = payload[coinbase_utils.PAYLOAD_DATA][coinbase_utils.PAYLOAD_ADDRESS]
        paid_at = parse(payload[coinbase_utils.PAYLOAD_DATA][coinbase_utils.PAYLOAD_CREATED_AT], ignoretz=True)
        amount = payload[coinbase_utils.PAYLOAD_ADDITIONAL_DATA][coinbase_utils.PAYLOAD_AMOUNT][coinbase_utils.PAYLOAD_AMOUNT]

        try:
            task = Task.objects.get(btc_address=address)
        except:
            task = None

        if task:
            TaskPayment.objects.get_or_create(
                task=task, ref=id, payment_type=TASK_PAYMENT_METHOD_BITCOIN, btc_address=task.btc_address, defaults={
                    'btc_received': amount, 'btc_price': task.btc_price, 'received_at': paid_at
                }
            )
            task.paid = True
            task.paid_at = paid_at
            task.save()

            distribute_task_payment.delay(task.id)
    return Response('Received')


@csrf_exempt
@api_view(http_method_names=['POST'])
@permission_classes([AllowAny])
def bitpesa_notification(request):
    # Verify that the request came from bitpesa
    bp_signature = request.META.get(bitpesa.HEADER_AUTH_SIGNATURE, None)
    bp_nonce = request.META.get(bitpesa.HEADER_AUTH_NONCE, None)
    if not bitpesa.verify_signature(bp_signature, request.build_absolute_uri(), request.method, request.data, bp_nonce):
        return Response('Unauthorized Request', status=status.HTTP_401_UNAUTHORIZED)

    payload = request.data
    if payload:
        transaction = payload.get(bitpesa.KEY_OBJECT, None)
        if transaction and payload.get(bitpesa.KEY_EVENT, None) == bitpesa.EVENT_TRANSACTION_APPROVED:
            if complete_bitpesa_payment(transaction):
                return Response('Received')
    return Response('Failed to process', status=status.HTTP_400_BAD_REQUEST)
