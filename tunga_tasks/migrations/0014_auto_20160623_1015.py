# -*- coding: utf-8 -*-
# Generated by Django 1.9.6 on 2016-06-23 10:15
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tunga_tasks', '0013_auto_20160621_0823'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='progressevent',
            options={'ordering': ['due_at']},
        ),
    ]
