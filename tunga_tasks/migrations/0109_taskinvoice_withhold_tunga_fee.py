# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-06-14 10:33
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tunga_tasks', '0108_task_withhold_tunga_fee'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskinvoice',
            name='withhold_tunga_fee',
            field=models.BooleanField(default=False, help_text='Only participant portion will be paid if True, and all money paid will be distributed to participants'),
        ),
    ]
