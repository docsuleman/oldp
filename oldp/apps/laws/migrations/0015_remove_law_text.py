# Generated by Django 2.1.2 on 2018-10-25 10:20

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('laws', '0014_auto_20181025_1014'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='law',
            name='text',
        ),
    ]
