# -*- coding: utf-8 -*-
from dbaas_credentials.credential import Credential
from dbaas_credentials.models import CredentialType
from dbaas_zabbix import factory_for
from workflow.steps.util.base import BaseInstanceStep


class ZabbixStep(BaseInstanceStep):

    def __init__(self, instance):
        super(ZabbixStep, self).__init__(instance)

        integration = CredentialType.objects.get(type=CredentialType.ZABBIX)
        environment = self.instance.databaseinfra.environment
        self.credentials = Credential.get_credentials(environment, integration)
        self.instances = self.instance.hostname.instances.all()

        self.zabbix_provider = factory_for(
            databaseinfra=self.instance.databaseinfra,
            credentials=self.credentials
        )

    def __del__(self):
        self.zabbix_provider.logout()

    def do(self):
        raise NotImplementedError

    def undo(self):
        pass


class DestroyAlarms(ZabbixStep):

    def __unicode__(self):
        return "Destroying Zabbix alarms..."

    @property
    def hosts_in_zabbix(self):
        monitors = []
        monitors.append(self.instance.hostname.hostname)

        for instance in self.instances:
            current_dns = instance.dns
            monitors.append(current_dns)

            zabbix_extras = self.zabbix_provider.get_zabbix_databases_hosts()
            for zabbix_extra in zabbix_extras:
                if current_dns in zabbix_extra and zabbix_extra != current_dns:
                    monitors.append(zabbix_extra)

        return monitors

    def do(self):
        for host in self.hosts_in_zabbix:
            monitors = self.zabbix_provider.get_host_triggers(host)

            if monitors:
                self.zabbix_provider.delete_instance_monitors(host)


class CreateAlarms(ZabbixStep):

    def __unicode__(self):
        return "Creating Zabbix alarms..."

    def do(self):
        DestroyAlarms(self.instance).do()
        engine_version = self.instance.databaseinfra.plan.engine_equivalent_plan.engine.version
        zabbix_provider = factory_for(
            databaseinfra=self.instance.databaseinfra,
            credentials=self.credentials,
            engine_version=engine_version
        )
        zabbix_provider.create_instance_basic_monitors(
            self.instance.hostname
        )

        for instance in self.instances:
            zabbix_provider.create_instance_monitors(instance)


class DisableAlarms(ZabbixStep):

    def __unicode__(self):
        return "Disabling Zabbix alarms..."

    def do(self):
        self.zabbix_provider.disable_alarms()


class EnableAlarms(ZabbixStep):

    def __unicode__(self):
        return "Enabling Zabbix alarms..."

    def do(self):
        self.zabbix_provider.enable_alarms()
