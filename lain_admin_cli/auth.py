# -*- coding: utf-8 -*-

import time
import etcd
import json
import hashlib
import requests
from os import environ
from argh.decorators import arg, expects_obj
from lain_admin_cli.helpers import TwoLevelCommandBase
from subprocess import check_output, call
from lain_admin_cli.helpers import info, error, sso_login


class Auth(TwoLevelCommandBase):

    @classmethod
    def subcommands(self):
        return [self.init, self.open, self.close]

    @classmethod
    def namespace(self):
        return "auth"

    @classmethod
    def help_message(self):
        return "lain auth operations"

    @classmethod
    @expects_obj
    @arg('-c', '--cid', default='3', help="Client id get from the sso system.")
    @arg('-s', '--secret', default='lain-cli_admin', help="Client secret get from the sso system.")
    @arg('-r', '--redirect_uri', default='https://example.com/', help="Redirect uri get from the sso system.")
    @arg('-u', '--sso_url', default='http://sso.lain.local', help="The sso_url need to be process")
    @arg('-a', '--check_all', default='False', help="Whether check all apps to create app groups in sso")
    def init(self, args):
        '''
        init the auth of lain, create groups in sso for lain apps
        '''
        login_success, token = sso_login(args.sso_url, args.cid, args.secret, args.redirect_uri)
        if login_success:
            add_sso_groups(args.sso_url, token, args.check_all)
        else:
            error("login failed.")
            exit(1)


    @classmethod
    @expects_obj
    @arg('-s', '--scope', default='all', choices=['console', 'all'])
    @arg('-t', '--type', default='lain-sso', help='The auth type for console')
    @arg('-u', '--url', default='http://sso.lain.local', help='the auth url for console')
    @arg('-r', '--realm', default='http://console.lain.local/api/v1/authorize/registry/',
         help='the realm in which the registry server authenticates')
    @arg('-i', '--issuer', default='auth server', help='the name of registry token issuer')
    def open(self, args):
        '''
        open the auth of lain
        '''
        scope = args.scope
        info("ready to open auth of %s" % scope)
        if scope != 'all':
            open_ops[scope](args)
        else:
            for _, op in open_ops.iteritems():
                op(args)

    @classmethod
    @expects_obj
    @arg('-s', '--scope', default='all', choices=['registry', 'all'])
    def close(self, args):
        '''
        close the auth of lain
        '''
        scope = args.scope
        info("ready to close auth of %s" % scope)
        if scope != 'all':
            close_ops[scope]()
        else:
            for _, op in close_ops.iteritems():
                op()


def add_sso_groups(sso_url, token, check_all):
    if check_all != 'True':
        appnames = ['console', 'registry', 'tinydns', 'webrouter', 'lvault']
        get_apps_success = True
    else:
        get_apps_success, appnames = get_console_apps(token)
    if not get_apps_success:
        return
    for app in appnames:
        try:
            group_prefix = "ca"
            appname_prefix = environ.get("SSO_GROUP_NAME_PREFIX", "ConsoleApp" + get_console_domain())
            group_fullname_prefix = environ.get("SSO_GROUP_FULLNAME_PREFIX", "Console APP in %s: "%get_console_domain())
            # the first character of groups in sso needs to be [a-zA-Z], be the same with console rule
            group_name = "%s%s" %(group_prefix, hashlib.md5(appname_prefix + app).hexdigest()[0:30])
            group_fullname = "%s%s" % (group_fullname_prefix, app)
            group_msg = {'name' : group_name, 'fullname' : group_fullname}
            headers = {"Content-Type":"application/json", "Accept":"application/json", 'Authorization' : 'Bearer %s'%token}
            url = "%s/api/groups/" %sso_url
            req = requests.request("POST", url, headers=headers, json=group_msg, verify=False)
            if req.status_code == 201:
                info("successfully create sso group for app %s" %app)
            else:
                result = req.text
                print("create sso group for app %s wrong: %s" %(app, result.encode('utf8')))
            time.sleep(3)
        except Exception as e:
            print("create sso group for app %s wrong: %s" %(app, e))


def get_console_apps(token):
    appnames = []
    try:
        url = "http://console.%s/api/v1/repos/" % get_console_domain()
        headers = {"Content-Type": "application/json", "access-token": token}
        req = requests.get(url, headers=headers)
        apps = json.loads(req.text)['repos']
        for app in apps:
            appnames.append(app['appname'])
        return True, appnames
    except Exception as e:
        print("Get console apps error: %s" %e)
        return False, appnames


def get_console_domain():
    try:
        etcd_authority = environ.get("CONSOLE_ETCD_HOST", "etcd.lain:4001")
        client = get_etcd_client(etcd_authority)
        domain = client.read("/lain/config/domain").value
        return domain
    except Exception:
        raise Exception("unable to get the console domain!")


def get_etcd_client(etcd_authority):
    etcd_host_and_port = etcd_authority.split(":")
    if len(etcd_host_and_port) == 2:
        return etcd.Client(host=etcd_host_and_port[0], port=int(etcd_host_and_port[1]))
    elif len(etcd_host_and_port) == 1:
        return etcd.Client(host=etcd_host_and_port[0], port=4001)
    else:
        raise Exception("invalid ETCD_AUTHORITY : %s" % etcd_authority)


def open_console_auth(args):
    info("opening console auth...")
    auth_setting = '{"type": "%s", "url": "%s"}' % (args.type, args.url)

    check_output(['etcdctl', 'set',
                  '/lain/config/auth/console',
                  auth_setting])


def close_console_auth():
    info("closing console auth...")
    call(['etcdctl', 'rm',
          '/lain/config/auth/console'],
         stderr=open('/dev/null', 'w'))


def open_registry_auth(args):
    info("opening registry auth...")
    auth_setting = '{"realm": "%s", "issuer": "%s", "service": "lain.local"}' % (
        args.realm, args.issuer)

    check_output(['etcdctl', 'set',
                  '/lain/config/auth/registry',
                  auth_setting])
    __restart_registry()


def close_registry_auth():
    info("closing registry auth...")
    call(['etcdctl', 'rm',
          '/lain/config/auth/registry'],
         stderr=open('/dev/null', 'w'))
    __restart_registry()


def __restart_registry():
    info("restarting registry...")
    try:
        container_id = check_output(['docker', '-H', ':2376', 'ps', '-qf', 'name=registry.web.web']).strip()
        info("container id of registry is : %s" % container_id)
        check_output(['docker', '-H', ':2376', 'stop', container_id])
        time.sleep(3)
        check_output(['docker', '-H', ':2376', 'start', container_id])
    except Exception as e:
        error("restart registry failed : %s, please try again or restart it manually." % str(e))


open_ops = {
    'console': open_console_auth,
    'registry': open_registry_auth
}

close_ops = {
    'console': close_console_auth,
    'registry': close_registry_auth
}
