# -*- coding: utf-8 -*-
# Generated by Django 1.9.7 on 2016-09-26 13:25
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tunga_profiles', '0018_auto_20160925_1414'),
    ]

    operations = [
        migrations.CreateModel(
            name='Inquirer',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50)),
                ('email', models.EmailField(blank=True, max_length=254, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AlterUniqueTogether(
            name='appintegration',
            unique_together=set([('user', 'provider')]),
        ),
    ]
