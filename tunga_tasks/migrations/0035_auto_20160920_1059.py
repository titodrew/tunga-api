# -*- coding: utf-8 -*-
# Generated by Django 1.9.7 on 2016-09-20 10:59
from __future__ import unicode_literals

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tunga_tasks', '0034_auto_20160806_1523'),
    ]

    operations = [
        migrations.AlterField(
            model_name='task',
            name='fee',
            field=models.DecimalField(decimal_places=4, max_digits=19, validators=[django.core.validators.MinValueValidator(15, message='Minimum pledge amount is EUR 15')]),
        ),
    ]
