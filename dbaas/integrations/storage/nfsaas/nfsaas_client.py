# -*- coding: utf-8 -*-
import urllib2
import json
import nfsaas_settings

class Nfsaas(object):
    
    def __init__(self, *args, **kwargs):
        
        self.base_url = nfsaas_settings.NFSAAS_URL
        self.teamid = nfsaas_settings.NFSAAS_TEAMID
        self.projectid = nfsaas_settings.NFSAAS_PROJECTID
        
        p = urllib2.HTTPPasswordMgrWithDefaultRealm()
        p.add_password(None, self.base_url, nfsaas_settings.NFSAAS_USERNAME, nfsaas_settings.NFSAAS_PASSWORD)
        handler = urllib2.HTTPBasicAuthHandler(p)
        opener = urllib2.build_opener(handler)
        urllib2.install_opener(opener)
    
    def list_teams(self):
        url = '%stimes/' % (self.base_url)
        return json.load(urllib2.urlopen(url))
    
    def list_projects(self):
        url = '%stimes/%s/projetos/' % (self.base_url, self.teamid)
        return json.load(urllib2.urlopen(url))
    
    def list_environments(self):
        url = '%stimes/%s/projetos/%s/ambientes/' % (self.base_url, self.teamid, self.projectid)
        return json.load(urllib2.urlopen(url))
    
    def list_sizes(self, environmentid):
        url = '%stimes/%s/projetos/%s/ambientes/%s/tamanhos/' % (
        self.base_url, self.teamid, self.projectid, environmentid)
        return json.load(urllib2.urlopen(url))
    
    def list_exports(self, environmentid, sizeid):
        url = '%stimes/%s/projetos/%s/ambientes/%s/tamanhos/%s/exports/' % (
        self.base_url, self.teamid, self.projectid, environmentid, sizeid)
        return json.load(urllib2.urlopen(url))

    def get_export(self, environmentid, sizeid, exportid):
        url = '%stimes/%s/projetos/%s/ambientes/%s/tamanhos/%s/exports/%s/' % (
        self.base_url, self.teamid, self.projectid, environmentid, sizeid, exportid)
        return json.loads(json.load(urllib2.urlopen(url)))

    def create_export(self, environmentid, sizeid):
        url = '%stimes/%s/projetos/%s/ambientes/%s/tamanhos/%s/exports/' % (
        self.base_url, self.teamid, self.projectid, environmentid, sizeid)
        request = urllib2.Request(url, data="{}")
        request.add_header("Content-Type", "application/json")
        newexport = json.load(urllib2.urlopen(request))
        newexport = json.loads(newexport[0])
        return newexport
        
    def drop_export(self, environmentid, sizeid, exportid):
        url = '%stimes/%s/projetos/%s/ambientes/%s/tamanhos/%s/exports/%s/' % (
        self.base_url, self.teamid, self.projectid, environmentid, sizeid, exportid)
        request = urllib2.Request(url)
        request.get_method = lambda: 'DELETE'
        deleted_export = urllib2.urlopen(request)
        return deleted_export
    
    def list_access(self, environmentid, sizeid, exportid):
        url = '%stimes/%s/projetos/%s/ambientes/%s/tamanhos/%s/exports/%s/acessos/' % (
        self.base_url, self.teamid, self.projectid, environmentid, sizeid, exportid)
        return json.load(urllib2.urlopen(url))

    def create_access(self, environmentid, sizeid, exportid, host):
        def change_ip(host):
            h = host.split('.')
            h =h[:len(h) - 1]
            return '.'.join(h) + '.0/24'
        host = change_ip(host)
        url = '%stimes/%s/projetos/%s/ambientes/%s/tamanhos/%s/exports/%s/acessos/' % (
        self.base_url, self.teamid, self.projectid, environmentid, sizeid, exportid)
        data="""{
            "hosts": "%s",
            "permission": {
                "type": "read-write"
            }     
        }""" % (host,)
        request = urllib2.Request(url, data=data)
        request.add_header("Content-Type", "application/json")
        newaccess = json.load(urllib2.urlopen(request))
        newaccess = json.loads(newaccess[0])
        return newaccess
    
    def drop_access(self, environmentid, sizeid, exportid, accessid):
        url = '%stimes/%s/projetos/%s/ambientes/%s/tamanhos/%s/exports/%s/acessos/%s/' % (
        self.base_url, self.teamid, self.projectid, environmentid, sizeid, exportid, accessid)
        request = urllib2.Request(url)
        request.get_method = lambda: 'DELETE'
        deleted_acesso = urllib2.urlopen(request)
        return deleted_acesso