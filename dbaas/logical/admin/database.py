# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals
import logging
import sys
from dex import dex
from cStringIO import StringIO
from functools import partial
from bson.json_util import loads
from django.db import IntegrityError
from django.utils.translation import ugettext_lazy as _
from django_services import admin
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.http import HttpResponseRedirect
from django.contrib.admin.util import flatten_fieldsets
from django.core.urlresolvers import reverse
from django.conf.urls import patterns, url
from django.contrib import messages
from django.utils.html import format_html, escape
from django.forms.models import modelform_factory
from django.db import router
from django.utils.encoding import force_text
from django.core.exceptions import PermissionDenied
from django.contrib.admin.util import get_deleted_objects, model_ngettext
from django.contrib.admin import helpers
from django.template.response import TemplateResponse
from django.core.exceptions import FieldError
from dbaas_credentials.models import CredentialType
from dbaas import constants
from account.models import Team
from drivers import DatabaseAlreadyExists
from notification.tasks import create_database, upgrade_database, resize_database
from notification.models import TaskHistory
from physical.models import Plan, Host, DiskOffering
from system.models import Configuration
from util import get_credentials_for
from util.html import show_info_popup
from logical.templatetags import capacity
from logical.models import Database
from logical.forms import DatabaseForm, CloneDatabaseForm, ResizeDatabaseForm, \
    DiskResizeDatabaseForm, RestoreDatabaseForm
from logical.validators import check_is_database_enabled, \
    check_is_database_dead, check_resize_options, \
    check_database_has_persistence
from logical.errors import DisabledDatabase, NoResizeOption
from logical.service.database import DatabaseService

LOG = logging.getLogger(__name__)


class DatabaseAdmin(admin.DjangoServicesAdmin):

    """
    the form used by this view is returned by the method get_form
    """

    database_add_perm_message = _(
        "You must be set to at least one team to add a database, and the service administrator has been notified about this.")
    perm_manage_quarantine_database = constants.PERM_MANAGE_QUARANTINE_DATABASE
    perm_add_database_infra = constants.PERM_ADD_DATABASE_INFRA

    service_class = DatabaseService
    search_fields = (
        "name", "databaseinfra__name", "team__name", "project__name",
        "environment__name", "databaseinfra__engine__engine_type__name"
    )
    list_display_basic = [
        "name_html", "team_admin_page", "engine_html", "environment", "offering_html",
        "friendly_status", "clone_html", "get_capacity_html", "metrics_html",
        "created_dt_format"
    ]
    list_display_advanced = list_display_basic + ["quarantine_dt_format"]
    list_filter_basic = [
        "project", "databaseinfra__environment", "databaseinfra__engine",
        "databaseinfra__plan", "databaseinfra__engine__engine_type", "status",
        "databaseinfra__plan__has_persistence"
    ]
    list_filter_advanced = list_filter_basic + ["is_in_quarantine", "team"]
    add_form_template = "logical/database/database_add_form.html"
    change_form_template = "logical/database/database_change_form.html"
    delete_button_name = "Delete"
    fieldsets_add = (
        (None, {
            'fields': (
                'name', 'description', 'project', 'environment', 'engine',
                'team', 'team_contact', 'subscribe_to_email_events', 'plan',
                'is_in_quarantine',
            )
        }
        ),
    )

    fieldsets_change_basic = (
        (None, {
            'fields': [
                'name', 'description', 'project', 'team', 'team_contact',
                'subscribe_to_email_events', 'is_protected', 'disk_auto_resize',
            ]
        }),
    )

    readonly_fields = ('team_contact',)

    fieldsets_change_advanced = (
        (None, {
            'fields': fieldsets_change_basic[0][1]['fields'] + ["backup_path", "is_in_quarantine"]
        }
        ),
    )
    # actions = ['delete_mode']

    def quarantine_dt_format(self, database):
        return database.quarantine_dt or ""

    quarantine_dt_format.short_description = "Quarantine since"
    quarantine_dt_format.admin_order_field = 'quarantine_dt'

    def created_dt_format(self, database):
        return database.created_at.strftime("%b. %d, %Y") or ""

    created_dt_format.short_description = "Created at"
    created_dt_format.admin_order_field = 'created_at'

    def environment(self, database):
        return database.environment

    environment.admin_order_field = 'name'

    def plan(self, database):
        return database.plan

    plan.admin_order_field = 'name'

    def friendly_status(self, database):

        html_default = '<span class="label label-{}">{}</span>'

        if database.status == Database.ALIVE:
            status = html_default.format("success", "Alive")
        elif database.status == Database.DEAD:
            status = html_default.format("important", "Dead")
        elif database.status == Database.ALERT:
            status = html_default.format("warning", "Alert")
        else:
            status = html_default.format("info", "Initializing")

        return format_html(status)

    friendly_status.short_description = "Status"

    def clone_html(self, database):
        html = []

        can_be_cloned, _ = database.can_be_cloned(database_view_button=True)
        if not can_be_cloned:
            html.append("N/A")
        else:
            html.append("<a class='btn btn-info' href='%s'><i class='icon-file icon-white'></i></a>" % reverse(
                'admin:database_clone', args=(database.id,)))

        return format_html("".join(html))

    clone_html.short_description = "Clone"

    def team_admin_page(self, database):
        team_name = database.team.name
        if self.list_filter == self.list_filter_advanced:
            url = reverse('admin:account_team_change',
                          args=(database.team.id,))
            team_name = """<a href="{}"> {} </a> """.format(url, team_name)
            return format_html(team_name)
        return team_name

    team_admin_page.short_description = "Team"

    def metrics_html(self, database):
        html = []
        if database.databaseinfra.plan.is_pre_provisioned:
            html.append("N/A")
        else:
            html.append("<a class='btn btn-info' href='%s'><i class='icon-list-alt icon-white'></i></a>" % reverse(
                'admin:database_metrics', args=(database.id,)))

        return format_html("".join(html))

    metrics_html.short_description = "Metrics"

    def description_html(self, database):

        html = []
        html.append("<ul>")
        html.append("<li>Engine Type: %s</li>" % database.engine_type)
        html.append("<li>Environment: %s</li>" % database.environment)
        html.append("<li>Plan: %s</li>" % database.plan)
        html.append("</ul>")

        return format_html("".join(html))

    description_html.short_description = "Description"

    def name_html(self, database):
        try:
            ed_point = escape(database.get_endpoint_dns())
        except:
            ed_point = None
        return show_info_popup(
            database.name, "Show Endpoint", ed_point,
            "icon-info-sign", "show-endpoint"
        )
    name_html.short_description = _("name")
    name_html.admin_order_field = "name"

    def engine_type(self, database):
        return database.engine_type
    engine_type.admin_order_field = 'name'

    def engine_html(self, database):
        engine_info = str(database.engine)

        topology = database.databaseinfra.plan.replication_topology
        if topology.details:
            engine_info += " - " + topology.details

        upgrades = database.upgrades.filter(source_plan=database.infra.plan)
        last_upgrade = upgrades.last()
        if not(last_upgrade and last_upgrade.is_status_error):
            return engine_info

        upgrade_url = reverse('admin:maintenance_databaseupgrade_change', args=[last_upgrade.id])
        task_url = reverse('admin:notification_taskhistory_change', args=[last_upgrade.task.id])
        retry_url = database.get_upgrade_retry_url()
        upgrade_content = \
            "<a href='{}' target='_blank'>Last upgrade</a> has an <b>error</b>, " \
            "please check the <a href='{}' target='_blank'>task</a> and " \
            "<a href='{}'>retry</a> the database upgrade".format(
                upgrade_url, task_url, retry_url
            )
        return show_info_popup(
            engine_info, "Database Upgrade", upgrade_content,
            icon="icon-warning-sign", css_class="show-upgrade"
        )
    engine_html.short_description = _("engine")
    engine_html.admin_order_field = "Engine"

    def offering_html(self, database):
        last_resize = database.resizes.last()
        if not(last_resize and last_resize.is_status_error):
            return database.offering

        resize_url = reverse('admin:maintenance_databaseresize_change', args=[last_resize.id])
        task_url = reverse('admin:notification_taskhistory_change', args=[last_resize.task.id])
        retry_url = database.get_resize_retry_url()
        resize_content = \
            "<a href='{}' target='_blank'>Last resize</a> has an <b>error</b>, " \
            "please check the <a href='{}' target='_blank'>task</a> and " \
            "<a href='{}'>retry</a> the database resize".format(
                resize_url, task_url, retry_url
            )
        return show_info_popup(
            database.offering, "Database Resize", resize_content,
            icon="icon-warning-sign", css_class="show-resize"
        )
    offering_html.short_description = _("offering")
    offering_html.admin_order_field = "Offering"

    def get_capacity_html(self, database):
        try:
            return capacity.render_capacity_html(database)
        except:
            return None

    get_capacity_html.short_description = "Capacity"

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        filter teams for the ones that the user is associated, unless the user has ther
        perm to add databaseinfra. In this case, he should see all teams.
        """
        if not request.user.has_perm(self.perm_add_database_infra):
            if db_field.name == "team":
                kwargs["queryset"] = Team.objects.filter(users=request.user)
        return super(DatabaseAdmin, self).formfield_for_foreignkey(db_field, request, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj:  # In edit mode
            if request.user.has_perm(self.perm_manage_quarantine_database):
                self.fieldsets_change = self.fieldsets_change_advanced
            else:
                self.fieldsets_change = self.fieldsets_change_basic

        return self.fieldsets_change if obj else self.fieldsets_add

    def get_readonly_fields(self, request, obj=None):
        """
        if in edit mode, name is readonly.
        """
        if obj:  # In edit mode
            # only sysadmin can change team accountable for a database
            if request.user.has_perm(self.perm_add_database_infra):
                return ('name', 'databaseinfra', ) + self.readonly_fields
            else:
                return ('name', 'databaseinfra', 'team',) + self.readonly_fields
        return self.readonly_fields

    def queryset(self, request):
        qs = super(DatabaseAdmin, self).queryset(request)
        if request.user.has_perm(self.perm_add_database_infra):
            return qs

        return qs.filter(team__in=[team.id for team in Team.objects.filter(users=request.user)])

    def has_add_permission(self, request):
        """User must be set to at least one team to be able to add database"""
        teams = Team.objects.filter(users=request.user)
        if not teams:
            self.message_user(
                request, self.database_add_perm_message, level=messages.ERROR)
            return False
        else:
            return super(DatabaseAdmin, self).has_add_permission(request)

    def get_form(self, request, obj=None, **kwargs):
        if 'fields' in kwargs:
            fields = kwargs.pop('fields')
        else:
            fields = flatten_fieldsets(self.get_fieldsets(request, obj))
        if self.exclude is None:
            exclude = []
        else:
            exclude = list(self.exclude)
        exclude.extend(self.get_readonly_fields(request, obj))
        if self.exclude is None and hasattr(self.form, '_meta') and self.form._meta.exclude:
            # Take the custom ModelForm's Meta.exclude into account only if the
            # ModelAdmin doesn't define its own.
            exclude.extend(self.form._meta.exclude)
        # if exclude is an empty list we pass None to be consistent with the
        # default on modelform_factory
        exclude = exclude or None

        if obj and obj.plan.provider == Plan.CLOUDSTACK:
            if 'offering' in self.fieldsets_change[0][1]['fields'] and 'offering' in self.form.declared_fields:
                del self.form.declared_fields['offering']
            else:
                self.fieldsets_change[0][1]['fields'].append('offering')
            DatabaseForm.setup_offering_field(form=self.form, db_instance=obj)

        if obj:
            if 'disk_offering' in self.fieldsets_change[0][1]['fields']:
                self.fieldsets_change[0][1]['fields'].remove('disk_offering')

            self.fieldsets_change[0][1]['fields'].append('disk_offering')
            DatabaseForm.setup_disk_offering_field(
                form=self.form, db_instance=obj
            )

        defaults = {
            "form": self.form,
            "fields": fields,
            "exclude": exclude,
            "formfield_callback": partial(self.formfield_for_dbfield, request=request),
        }
        defaults.update(kwargs)

        try:
            return modelform_factory(self.model, **defaults)
        except FieldError as e:
            raise FieldError('%s. Check fields/fieldsets/exclude attributes of class %s.'
                             % (e, self.__class__.__name__))

    def changelist_view(self, request, extra_context=None):
        if request.user.has_perm(self.perm_manage_quarantine_database):
            self.list_display = self.list_display_advanced
        else:
            self.list_display = self.list_display_basic

        if request.user.has_perm(self.perm_add_database_infra):
            self.list_filter = self.list_filter_advanced
        else:
            self.list_filter = self.list_filter_basic

        return super(DatabaseAdmin, self).changelist_view(request, extra_context=extra_context)

    def add_view(self, request, form_url='', extra_context=None):
        self.form = DatabaseForm

        try:

            if request.method == 'POST':

                teams = Team.objects.filter(users=request.user)
                LOG.info("user %s teams: %s" % (request.user, teams))
                if not teams:
                    self.message_user(
                        request, self.database_add_perm_message,
                        level=messages.ERROR
                    )
                    return HttpResponseRedirect(
                        reverse('admin:logical_database_changelist')
                    )

                # if no team is specified and the user has only one team, then
                # set it to the database
                if teams.count() == 1 and request.method == 'POST' and not request.user.has_perm(
                        self.perm_add_database_infra):

                    post_data = request.POST.copy()
                    if 'team' in post_data:
                        post_data['team'] = u"%s" % teams[0].pk

                    request.POST = post_data

                form = DatabaseForm(request.POST)

                if not form.is_valid():
                    return super(DatabaseAdmin, self).add_view(request, form_url, extra_context=extra_context)

                database_creation_message = "call create_database - name={}, plan={}, environment={}, team={}, project={}, description={}, user={}, subscribe_to_email_events {}".format(
                    form.cleaned_data['name'], form.cleaned_data['plan'],
                    form.cleaned_data['environment'], form.cleaned_data['team'],
                    form.cleaned_data['project'], form.cleaned_data['description'],
                    request.user, form.cleaned_data['subscribe_to_email_events']
                )
                LOG.debug(database_creation_message)

                task_history = TaskHistory()
                task_history.task_name = "create_database"
                task_history.task_status = task_history.STATUS_WAITING
                task_history.arguments = "Database name: {}".format(
                    form.cleaned_data['name'])
                task_history.user = request.user
                task_history.save()

                create_database.delay(
                    name=form.cleaned_data['name'],
                    plan=form.cleaned_data['plan'],
                    environment=form.cleaned_data['environment'],
                    team=form.cleaned_data['team'],
                    project=form.cleaned_data['project'],
                    description=form.cleaned_data['description'],
                    subscribe_to_email_events=form.cleaned_data['subscribe_to_email_events'],
                    task_history=task_history,
                    user=request.user
                )

                url = reverse('admin:notification_taskhistory_changelist')
                # Redirect after POST
                return HttpResponseRedirect(url + "?user=%s" % request.user.username)

            else:
                return super(DatabaseAdmin, self).add_view(request, form_url, extra_context=extra_context)

        except DatabaseAlreadyExists:
            self.message_user(request, _(
                'An inconsistency was found: The database "%s" already exists in infra-structure but not in DBaaS.') %
                request.POST['name'], level=messages.ERROR)

            request.method = 'GET'
            return super(DatabaseAdmin, self).add_view(request, form_url, extra_context=extra_context)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        database = Database.objects.get(id=object_id)
        self.form = DatabaseForm
        extra_context = extra_context or {}

        extra_context['has_perm_upgrade_mongo'] = False
        extra_context['can_upgrade'] = False

        if database.is_mongodb_24():
            extra_context['has_perm_upgrade_mongo'] = request.user.has_perm(constants.PERM_UPGRADE_MONGO24_TO_30)
        else:
            has_permission = request.user.has_perm(
                constants.PERM_UPGRADE_DATABASE
            )
            has_equivalent_plan = bool(
                database.infra.plan.engine_equivalent_plan
            )
            extra_context['can_upgrade'] = has_equivalent_plan and has_permission

        upgrades = database.upgrades.filter(source_plan=database.infra.plan)
        last_upgrade = upgrades.last()
        extra_context['last_upgrade'] = last_upgrade
        extra_context['retry_upgrade'] = False
        if last_upgrade:
            extra_context['retry_upgrade'] = last_upgrade.is_status_error

        if database.is_in_quarantine:
            extra_context['delete_button_name'] = self.delete_button_name
        else:
            extra_context['delete_button_name'] = "Delete"

        if request.user.team_set.filter(role__name="role_dba"):
            extra_context['is_dba'] = True
        else:
            extra_context['is_dba'] = False

        if request.method == 'POST':
            form = DatabaseForm(request.POST)
            if not form.is_valid():
                return super(DatabaseAdmin, self).change_view(
                    request, object_id, form_url, extra_context=extra_context
                )

        return super(DatabaseAdmin, self).change_view(
            request, object_id, form_url, extra_context=extra_context
        )

    def delete_view(self, request, object_id, extra_context=None):
        database = Database.objects.get(id=object_id)

        can_be_deleted, error = database.can_be_deleted()
        if not can_be_deleted:
            self.message_user(request, error, level=messages.ERROR)
            url = '/admin/logical/database/{}/'.format(object_id)
            return HttpResponseRedirect(url)

        extra_context = extra_context or {}
        if not database.is_in_quarantine:
            extra_context['quarantine_days'] = Configuration.get_by_name_as_int('quarantine_retention_days')

        return super(DatabaseAdmin, self).delete_view(request, object_id, extra_context=extra_context)

    def delete_model(modeladmin, request, obj):
        LOG.debug("Deleting {}".format(obj))
        database = obj

        can_be_deleted, error = database.can_be_deleted()
        if not can_be_deleted:
            modeladmin.message_user(request, error, level=messages.ERROR)
            url = reverse('admin:logical_database_changelist')
            return HttpResponseRedirect(url)

        database.destroy(request.user)

    def clone_view(self, request, database_id):
        database = Database.objects.get(id=database_id)

        can_be_cloned, error = database.can_be_cloned()
        if not can_be_cloned:
            self.message_user(request, error, level=messages.ERROR)
            url = reverse('admin:logical_database_changelist')
            return HttpResponseRedirect(url)

        if database.is_beeing_used_elsewhere():
            self.message_user(
                request, "Database cannot be cloned because it is in use by another task.", level=messages.ERROR)
            url = reverse('admin:logical_database_changelist')
            return HttpResponseRedirect(url)

        form = None
        if request.method == 'POST':  # If the form has been submitted...
            # A form bound to the POST data
            form = CloneDatabaseForm(request.POST)
            if form.is_valid():  # All validation rules pass
                # Process the data in form.cleaned_data
                database_clone = form.cleaned_data['database_clone']
                plan = form.cleaned_data['plan']
                environment = form.cleaned_data['environment']

                Database.clone(database=database, clone_name=database_clone,
                               plan=plan, environment=environment,
                               user=request.user
                               )

                url = reverse('admin:notification_taskhistory_changelist')
                # Redirect after POST
                return HttpResponseRedirect(url + "?user=%s" % request.user.username)
        else:
            form = CloneDatabaseForm(
                initial={"origin_database_id": database_id})  # An unbound form
        return render_to_response("logical/database/clone.html",
                                  locals(),
                                  context_instance=RequestContext(request))

    def metrics_view(self, request, database_id):
        database = Database.objects.get(id=database_id)

        if 'hostname' in request.GET:
            hostname = request.GET.get('hostname')
        else:
            instance = database.infra.instances.all()[0]
            hostname = instance.hostname.hostname.split('.')[0]

        return self.database_host_metrics_view(request, database, hostname)

    def database_host_metrics_view(self, request, database, hostname):
        title = "{} Metrics".format(database.name)
        instance = database.infra.instances.filter(
            hostname__hostname__contains=hostname
        ).first()

        hosts = []
        for host in Host.objects.filter(instances__databaseinfra=database.infra).distinct():
            hosts.append(host.hostname.split('.')[0])

        credential = get_credentials_for(
            environment=database.databaseinfra.environment,
            credential_type=CredentialType.GRAFANA
        )
        grafana_url = '{}/dashboard/{}?{}={}&{}={}&{}={}'.format(
            credential.endpoint,
            credential.project.format(database.engine_type),
            credential.get_parameter_by_name('db_param'), instance.dns,
            credential.get_parameter_by_name('os_param'), instance.hostname.hostname,
            credential.get_parameter_by_name('env_param'),
            credential.get_parameter_by_name('environment')
        )

        return render_to_response(
            "logical/database/metrics/grafana.html",
            locals(),
            context_instance=RequestContext(request)
        )

    def database_dex_analyze_view(self, request, database_id):
        import json
        import random
        import os
        import string
        from datetime import datetime, timedelta

        def generate_random_string(length, stringset=string.ascii_letters + string.digits):
            return ''.join([stringset[i % len(stringset)]
                            for i in [ord(x) for x in os.urandom(length)]])

        database = Database.objects.get(id=database_id)

        if database.status != Database.ALIVE or not database.database_status.is_alive:
            self.message_user(
                request, "Database is not alive cannot be analyzed", level=messages.ERROR)
            url = reverse('admin:logical_database_changelist')
            return HttpResponseRedirect(url)

        if database.is_beeing_used_elsewhere():
            self.message_user(
                request, "Database cannot be analyzed because it is in use by another task.", level=messages.ERROR)
            url = reverse('admin:logical_database_changelist')
            return HttpResponseRedirect(url)

        parsed_logs = ''

        arq_path = Configuration.get_by_name(
            'database_clone_dir') + '/' + database.name + generate_random_string(20) + '.txt'

        arq = open(arq_path, 'w')
        arq.write(parsed_logs)
        arq.close()

        uri = 'mongodb://{}:{}@{}:{}/admin'.format(database.databaseinfra.user,
                                                   database.databaseinfra.password,
                                                   database.databaseinfra.instances.all()[
                                                       0].address,
                                                   database.databaseinfra.instances.all()[0].port)

        old_stdout = sys.stdout
        sys.stdout = mystdout = StringIO()

        md = dex.Dex(db_uri=uri, verbose=False, namespaces_list=[],
                     slowms=0, check_indexes=True, timeout=0)

        md.analyze_logfile(arq_path)

        sys.stdout = old_stdout

        dexanalyzer = loads(
            mystdout.getvalue().replace("\"", "&&").replace("'", "\"").replace("&&", "'"))

        os.remove(arq_path)

        import ast
        final_mask = """<div>"""

        for result in dexanalyzer['results']:

            final_mask += "<h3> Collection: " + result['namespace'] + "</h3>"
            final_mask += \
                """<li> Query: """ +\
                str(ast.literal_eval(result['queryMask'])['$query']) +\
                """</li>""" +\
                """<li> Index: """ +\
                result['recommendation']['index'] +\
                """</li>""" +\
                """<li> Command: """ +\
                result['recommendation']['shellCommand'] +\
                """</li>"""

            final_mask += """<br>"""

        final_mask += """</ul> </div>"""

        return render_to_response("logical/database/dex_analyze.html", locals(), context_instance=RequestContext(request))

    def database_resize_view(self, request, database_id):
        try:
            check_is_database_dead(database_id, 'VM resize')
            database = check_is_database_enabled(database_id, 'VM resize')

            from dbaas_cloudstack.models import CloudStackPack
            offerings = CloudStackPack.objects.filter(
                offering__region__environment=database.environment,
                engine_type__name=database.engine_type
            ).exclude(offering__serviceofferingid=database.offering_id)
            check_resize_options(database_id, offerings)

        except (DisabledDatabase, NoResizeOption) as err:
            self.message_user(request, err.message, messages.ERROR)
            return HttpResponseRedirect(err.url)

        form = None
        if request.method == 'POST':  # If the form has been submitted...
            form = ResizeDatabaseForm(request.POST, initial={
                                      "database_id": database_id, "original_offering_id": database.offering_id},)  # A form bound to the POST data
            if form.is_valid():  # All validation rules pass

                cloudstackpack = CloudStackPack.objects.get(
                    id=request.POST.get('target_offer'))
                Database.resize(database=database, cloudstackpack=cloudstackpack,
                                user=request.user,)

                url = reverse('admin:notification_taskhistory_changelist')

                # Redirect after POST
                return HttpResponseRedirect(url + "?user=%s" % request.user.username)
        else:
            form = ResizeDatabaseForm(initial={
                                      "database_id": database_id, "original_offering_id": database.offering_id},)  # An unbound form
        return render_to_response("logical/database/resize.html",
                                  locals(),
                                  context_instance=RequestContext(request))

    def resize_retry(self, request, database_id):
        from dbaas_cloudstack.models import DatabaseInfraOffering
        database = Database.objects.get(id=database_id)

        can_do_resize, error = database.can_do_resize_retry()
        if can_do_resize:
            offering = DatabaseInfraOffering.objects.get(
                databaseinfra=database.databaseinfra
            ).offering
            last_resize = database.resizes.latest('created_at')

            if not last_resize.is_status_error:
                error = "Cannot do retry, last resize status is '{}'!".format(
                    last_resize.get_status_display()
                )
            else:
                current_step = last_resize.current_step

        if error:
            url = reverse('admin:logical_database_change', args=[database.id])
            self.message_user(request, error, level=messages.ERROR)
            return HttpResponseRedirect(url)

        task_history = TaskHistory()
        task_history.task_name = "resize_database_retry"
        task_history.task_status = task_history.STATUS_WAITING
        task_history.arguments = "Retrying resize database {}".format(database)
        task_history.user = request.user
        task_history.save()

        resize_database.delay(
            database=database, user=request.user, task=task_history,
            cloudstackpack=last_resize.target_offer,
            original_cloudstackpack=last_resize.source_offer,
            since_step=current_step
        )

        url = reverse('admin:notification_taskhistory_changelist')
        return HttpResponseRedirect(url)

    def database_disk_resize_view(self, request, database_id):
        try:
            database = check_is_database_enabled(database_id, 'disk resize')
            offerings = DiskOffering.objects.all().exclude(
                id=database.databaseinfra.disk_offering.id
            )
            check_resize_options(database_id, offerings)
        except (DisabledDatabase, NoResizeOption) as err:
            self.message_user(request, err.message, messages.ERROR)
            return HttpResponseRedirect(err.url)

        form = None
        if request.method == 'POST':
            form = DiskResizeDatabaseForm(database=database, data=request.POST)
            if form.is_valid():
                Database.disk_resize(
                    database=database,
                    new_disk_offering=request.POST.get('target_offer'),
                    user=request.user
                )

                url = reverse('admin:notification_taskhistory_changelist')
                return HttpResponseRedirect(
                    "{}?user={}".format(url, request.user.username)
                )
        else:
            form = DiskResizeDatabaseForm(database=database)

        return render_to_response("logical/database/disk_resize.html",
                                  locals(),
                                  context_instance=RequestContext(request))

    def restore_snapshot(self, request, database_id):
        database = Database.objects.get(id=database_id)

        url = reverse('admin:logical_database_change', args=[database.id])

        if not database.restore_allowed():
            self.message_user(
                request,
                "Restore is not allowed. Please, contact DBaaS team for more information",
                level=messages.WARNING
            )
            return HttpResponseRedirect(url)

        if database.is_in_quarantine:
            self.message_user(request, "Database in quarantine and cannot be restored", level=messages.ERROR)
            return HttpResponseRedirect(url)

        if database.status != Database.ALIVE or not database.database_status.is_alive:
            self.message_user(request, "Database is dead and cannot be restored", level=messages.ERROR)
            return HttpResponseRedirect(url)

        if database.is_beeing_used_elsewhere():
            self.message_user(
                request,
                "Database is beeing used by another task, please check your tasks",
                level=messages.ERROR
            )
            return HttpResponseRedirect(url)

        if database.has_flipperfox_migration_started():
            self.message_user(
                request,
                "Database {} cannot be restored because it is beeing migrated.".format(database.name),
                level=messages.ERROR
            )
            url = reverse('admin:logical_database_changelist')
            return HttpResponseRedirect(url)

        form = None
        if request.method == 'POST':
            form = RestoreDatabaseForm(
                request.POST, initial={"database_id": database_id},)
            if form.is_valid():
                target_snapshot = request.POST.get('target_snapshot')

                task_history = TaskHistory()
                task_history.task_name = "restore_snapshot"
                task_history.task_status = task_history.STATUS_WAITING
                task_history.arguments = "Restoring {} to an older version.".format(
                    database.name)
                task_history.user = request.user
                task_history.save()

                Database.recover_snapshot(database=database,
                                          snapshot=target_snapshot,
                                          user=request.user,
                                          task_history=task_history.id)

                url = reverse('admin:notification_taskhistory_changelist')

                return HttpResponseRedirect(url + "?user=%s" % request.user.username)
        else:
            form = RestoreDatabaseForm(initial={"database_id": database_id, })

        return render_to_response("logical/database/restore.html",
                                  locals(),
                                  context_instance=RequestContext(request))

    def database_log_view(self, request, database_id):

        database = Database.objects.get(id=database_id)
        instance = database.infra.instances.all()[0]

        return render_to_response("logical/database/lognit.html",
                                  locals(),
                                  context_instance=RequestContext(request))

    def initialize_flipperfox_migration(self, request, database_id):
        from flipperfox_migration.models import DatabaseFlipperFoxMigration

        database = Database.objects.get(id=database_id)
        url = reverse(
            'admin:flipperfox_migration_databaseflipperfoxmigration_changelist')

        flipperfox_migration = DatabaseFlipperFoxMigration(database=database,
                                                           current_step=0,)

        if database.is_in_quarantine:
            self.message_user(
                request, "Database in quarantine and cannot be migrated", level=messages.ERROR)
            return HttpResponseRedirect(url)

        if database.status != Database.ALIVE or not database.database_status.is_alive:
            self.message_user(
                request, "Database is dead  and cannot be migrated", level=messages.ERROR)
            return HttpResponseRedirect(url)

        if database.has_flipperfox_migration_started():
            self.message_user(
                request, "Database {} is already migrating".format(database.name), level=messages.ERROR)
            return HttpResponseRedirect(url)

        try:
            flipperfox_migration.save()
            self.message_user(request, "Migration for {} started!".format(
                database.name), level=messages.SUCCESS)
        except IntegrityError, e:
            self.message_user(request, "Database {} is already migrating!".format(
                database.name), level=messages.ERROR)

        return HttpResponseRedirect(url)

    def mongodb_engine_version_upgrade(self, request, database_id):
        from notification.tasks import upgrade_mongodb_24_to_30

        url = reverse('admin:logical_database_change', args=[database_id])

        database = Database.objects.get(id=database_id)
        if database.is_in_quarantine:
            self.message_user(request, "Database in quarantine and cannot be upgraded!", level=messages.ERROR)
            return HttpResponseRedirect(url)

        if database.status != Database.ALIVE or not database.database_status.is_alive:
            self.message_user(request, "Database is dead and cannot be upgraded!", level=messages.ERROR)
            return HttpResponseRedirect(url)

        if database.has_flipperfox_migration_started():
            self.message_user(
                request,
                "Database {} is being migrated and cannot be upgraded!".format(database.name),
                level=messages.ERROR
            )
            return HttpResponseRedirect(url)

        if not database.is_mongodb_24:
            self.message_user(
                request,
                "Database {} cannot be upgraded. Please contact you DBA".format(database.name),
                level=messages.ERROR
            )
            return HttpResponseRedirect(url)

        if not request.user.has_perm(constants.PERM_UPGRADE_MONGO24_TO_30):
            self.message_user(
                request,
                "You have no permissions to upgrade {}. Please, contact your DBA".format(database.name),
                level=messages.ERROR
            )
            return HttpResponseRedirect(url)

        task_history = TaskHistory()
        task_history.task_name = "upgrade_mongodb_24_to_30"
        task_history.task_status = task_history.STATUS_WAITING
        task_history.arguments = "Upgrading MongoDB 2.4 to 3.0"
        task_history.user = request.user
        task_history.save()

        upgrade_mongodb_24_to_30.delay(database=database, user=request.user, task_history=task_history)
        url = reverse('admin:notification_taskhistory_changelist')

        return HttpResponseRedirect(url)

    def upgrade_retry(self, request, database_id):
        database = Database.objects.get(id=database_id)

        can_do_upgrade, error = database.can_do_upgrade_retry()
        if can_do_upgrade:
            source_plan = database.databaseinfra.plan
            upgrades = database.upgrades.filter(source_plan=source_plan)
            last_upgrade = upgrades.last()
            if not last_upgrade:
                error = "Database does not have upgrades from {} {}!".format(
                    source_plan.engine.engine_type, source_plan.engine.version
                )
            elif not last_upgrade.is_status_error:
                error = "Cannot do retry, last upgrade status is '{}'!".format(
                    last_upgrade.get_status_display()
                )
            else:
                since_step = last_upgrade.current_step

        if error:
            url = reverse('admin:logical_database_change', args=[database.id])
            self.message_user(request, error, level=messages.ERROR)
            return HttpResponseRedirect(url)

        task_history = TaskHistory()
        task_history.task_name = "upgrade_database_retry"
        task_history.task_status = task_history.STATUS_WAITING
        task_history.arguments = "Retrying upgrade database {}".format(database)
        task_history.user = request.user
        task_history.save()

        upgrade_database.delay(
            database=database,
            user=request.user,
            task=task_history,
            since_step=since_step
        )

        url = reverse('admin:notification_taskhistory_changelist')
        return HttpResponseRedirect(url)

    def upgrade(self, request, database_id):
        database = Database.objects.get(id=database_id)

        can_do_upgrade, error = database.can_do_upgrade()
        if not can_do_upgrade:
            url = reverse('admin:logical_database_change', args=[database.id])
            self.message_user(request, error, level=messages.ERROR)
            return HttpResponseRedirect(url)

        task_history = TaskHistory()
        task_history.task_name = "upgrade_database"
        task_history.task_status = task_history.STATUS_WAITING
        task_history.arguments = "Upgrading database {}".format(database)
        task_history.user = request.user
        task_history.save()

        upgrade_database.delay(
            database=database,
            user=request.user,
            task=task_history
        )

        url = reverse('admin:notification_taskhistory_changelist')
        return HttpResponseRedirect(url)

    # Create your views here.
    def get_urls(self):
        urls = super(DatabaseAdmin, self).get_urls()
        my_urls = patterns(
            '',
            url(r'^/?(?P<database_id>\d+)/clone/$',
                self.admin_site.admin_view(self.clone_view),
                name="database_clone"),

            url(r'^/?(?P<database_id>\d+)/metrics/$',
                self.admin_site.admin_view(self.metrics_view),
                name="database_metrics"),

            url(r'^/?(?P<database_id>\d+)/resize/$',
                self.admin_site.admin_view(self.database_resize_view),
                name="database_resize"),

            url(r'^/?(?P<database_id>\d+)/disk_resize/$',
                self.admin_site.admin_view(
                    self.database_disk_resize_view),
                name="database_disk_resize"),

            url(r'^/?(?P<database_id>\d+)/lognit/$',
                self.admin_site.admin_view(self.database_log_view),
                name="database_resize"),

            url(r'^/?(?P<database_id>\d+)/dex/$',
                self.admin_site.admin_view(self.database_dex_analyze_view),
                name="database_dex_analyze_view"),

            url(r'^/?(?P<database_id>\d+)/restore/$',
                self.admin_site.admin_view(self.restore_snapshot),
                name="database_restore_snapshot"),

            url(r'^/?(?P<database_id>\d+)/initialize_flipperfox_migration/$',
                self.admin_site.admin_view(self.initialize_flipperfox_migration),
                name="database_initialize_flipperfox_migration"),

            url(r'^/?(?P<database_id>\d+)/mongodb_engine_version_upgrade/$',
                self.admin_site.admin_view(
                    self.mongodb_engine_version_upgrade),
                name="mongodb_engine_version_upgrade"),

            url(
                r'^/?(?P<database_id>\d+)/upgrade/$',
                self.admin_site.admin_view(self.upgrade), name="upgrade"
            ),

            url(
                r'^/?(?P<database_id>\d+)/upgrade_retry/$',
                self.admin_site.admin_view(self.upgrade_retry),
                name="upgrade_retry"
            ),

            url(
                r'^/?(?P<database_id>\d+)/resize_retry/$',
                self.admin_site.admin_view(self.resize_retry),
                name="resize_retry"
            ),
        )

        return my_urls + urls

    def delete_selected(self, request, queryset):
        opts = self.model._meta
        app_label = opts.app_label

        # Check that the user has delete permission for the actual model
        if not self.has_delete_permission(request):
            raise PermissionDenied

        using = router.db_for_write(self.model)

        # Populate deletable_objects, a data structure of all related objects that
        # will also be deleted.
        deletable_objects, perms_needed, protected = get_deleted_objects(
            queryset, opts, request.user, self.admin_site, using)

        # The user has already confirmed the deletion.
        # Do the deletion and return a None to display the change list view
        # again.
        if request.POST.get('post'):
            if perms_needed:
                raise PermissionDenied

            quarantine = any(
                result['is_in_quarantine'] is True for result in queryset.values('is_in_quarantine')
            )

            successful = 0
            for obj in queryset:
                obj_display = force_text(obj)
                self.log_deletion(request, obj, obj_display)

                # remove the object
                remove = self.delete_model(request, obj)
                if not isinstance(remove, HttpResponseRedirect):
                    successful += 1

            if successful:
                self.message_user(
                    request, "Successfully deleted {} of {} {}.".format(
                        successful, len(queryset),
                        model_ngettext(self.opts, len(queryset))
                    )
                )

            # Return None to display the change list page again.
            if quarantine:
                url = reverse('admin:notification_taskhistory_changelist')
                return HttpResponseRedirect(url + "?user=%s" % request.user.username)

            return None

        if len(queryset) == 1:
            objects_name = force_text(opts.verbose_name)
        else:
            objects_name = force_text(opts.verbose_name_plural)

        if perms_needed or protected:
            title = _("Cannot delete %(name)s") % {"name": objects_name}
        else:
            title = _("Are you sure?")

        context = {
            "title": title,
            "objects_name": objects_name,
            "deletable_objects": [deletable_objects],
            'queryset': queryset,
            "perms_lacking": perms_needed,
            "protected": protected,
            "opts": opts,
            "app_label": app_label,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
        }

        # Display the confirmation page

        return TemplateResponse(request, self.delete_selected_confirmation_template or [
            "admin/%s/%s/delete_selected_confirmation.html" % (
                app_label, opts.object_name.lower()),
            "admin/%s/delete_selected_confirmation.html" % app_label,
            "admin/delete_selected_confirmation.html"
        ], context, current_app=self.admin_site.name)
