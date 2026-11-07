"""One bounded OCI launch attempt; fail closed after an ambiguous previous run."""
import hashlib
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.request

import oci

ARM = 'VM.Standard.A1.Flex'
AMD = 'VM.Standard.E2.1.Micro'
SHAPE = ARM
NAME = 'oci-free-arm'
# This exact AMD success was audited (RUNNING + SSH) before the user asked
# to continue for ARM. It is not an unresolved ARM request.
REVIEWED_AMD_RUN = '40663927323'
REVIEWED_AMD_HEAD = '4f2f67e09615fa0c8726bbc80fd7b9e5257faaa0'
MARKER = 'oci-free-arm-instance'
RETRY_MARKER = 'Safe to retry: capacity unavailable'
RATE_LIMIT_MARKER = 'Safe to retry: rate limited until '
RATE_LIMIT_COOLDOWN = 1800
# Audited legacy run: its only launch returned HTTP 429 TooManyRequests.
# OCI was re-listed before recovery; no ARM instance was accepted.
REVIEWED_RATE_LIMIT_RUN = '41981028295'
REVIEWED_RATE_LIMIT_HEAD = '83614b9d1b2cc09b8b2e7b64565e9e4c68eb0131'
REQUEST_STEP = 'Request one instance'


def boot_size():
    return 100


def image_id():
    return os.environ['IMAGE_ID']


def output(**values):
    target = os.environ.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a') as f:
            for key, value in values.items():
                f.write(f'{key}={value}\n')


def summary(message):
    print(message)
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a') as f:
            f.write(message + '\n\n')


def github(path):
    req = urllib.request.Request(
        'https://api.github.com/' + path,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def previous_run_state(runs, jobs, current_run):
    previous = [r for r in runs if str(r['id']) != str(current_run)
                and r['status'] not in ('queued', 'waiting', 'pending', 'requested')]
    for latest in sorted(previous, key=lambda r: r.get('run_started_at') or r['created_at'], reverse=True):
        if latest['status'] != 'completed':
            return False, 0
        if (str(latest['id']) == REVIEWED_AMD_RUN
                and latest.get('head_sha') == REVIEWED_AMD_HEAD
                and latest.get('conclusion') == 'success'):
            return True, 0
        steps = [s for job in jobs(latest['id']) for s in job.get('steps', [])]
        requests = [s for s in steps if s['name'] == REQUEST_STEP]
        if not requests or all(s.get('conclusion') == 'skipped' for s in requests):
            # A skipped cooldown run must not erase an earlier retry deadline
            # or conceal a request whose result was uncertain.
            continue
        if not all(s.get('conclusion') == 'success' for s in requests):
            return False, 0
        if (str(latest['id']) == REVIEWED_RATE_LIMIT_RUN
                and latest.get('head_sha') == REVIEWED_RATE_LIMIT_HEAD
                and latest.get('run_attempt') == 1
                and latest.get('conclusion') == 'failure'
                and any(s['name'] == 'Manual review required' and s.get('conclusion') == 'failure'
                        for s in steps)):
            return True, 0
        for step in steps:
            if step['name'] == RETRY_MARKER and step.get('conclusion') == 'success':
                completed = step.get('completed_at')
                # Both cron and local dispatch can queue a run. Enforce the
                # ten-minute launch interval inside their shared workflow too.
                deadline = math.ceil(datetime.fromisoformat(completed.replace('Z', '+00:00')).timestamp()) + 600 if completed else 0
                return True, deadline
        for step in steps:
            if step['name'].startswith(RATE_LIMIT_MARKER) and step.get('conclusion') == 'success':
                value = step['name'][len(RATE_LIMIT_MARKER):]
                if value.isdigit() and int(value) > 0:
                    return True, int(value)
        return False, 0
    # If the bounded history consists only of skipped requests, do not assume
    # an older unresolved request or a long server cooldown has disappeared.
    return len(runs) < 100, 0


def previous_run_safe(runs, jobs, current_run):
    return previous_run_state(runs, jobs, current_run)[0]


def rate_limit_retry_at(headers, now=None):
    now = time.time() if now is None else now
    retry_at = now + RATE_LIMIT_COOLDOWN
    value = next((str(v) for k, v in (headers or {}).items() if k.lower() == 'retry-after'), '')
    try:
        server_deadline = now + int(value) if value.isdigit() else parsedate_to_datetime(value).timestamp()
        retry_at = max(retry_at, server_deadline)
    except (ValueError, TypeError, OverflowError):
        pass
    return math.ceil(retry_at)


def load_config():
    return {'user': os.environ['OCI_CLI_USER'],
            'tenancy': os.environ['OCI_CLI_TENANCY'],
            'region': os.environ['OCI_CLI_REGION'],
            'fingerprint': os.environ['OCI_CLI_FINGERPRINT'],
            'key_content': os.environ['OCI_CLI_KEY_CONTENT']}


def clients(config):
    options = {'retry_strategy': oci.retry.NoneRetryStrategy(), 'timeout': (15, 60)}
    return (oci.identity.IdentityClient(config, **options),
            oci.core.ComputeClient(config, **options),
            oci.core.VirtualNetworkClient(config, **options),
            oci.core.BlockstorageClient(config, **options))


def all_items(method, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(method, *args, **kwargs).data


def check():
    repo = os.environ.get('GITHUB_REPOSITORY')
    if repo:
        runs = github(f'repos/{repo}/actions/workflows/create-vm.yml/runs?per_page=100')['workflow_runs']
        def jobs(run_id):
            return github(f'repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100')['jobs']
        safe, retry_at = previous_run_state(runs, jobs, os.environ['GITHUB_RUN_ID'])
        if not safe:
            summary('BLOCKED: A previous request may have been accepted. Stopping for manual review; no new request sent.')
            output(status='blocked')
            return
        if time.time() < retry_at:
            summary(f'COOLDOWN: Waiting for the next permitted attempt. No OCI request sent; retry after Unix time {retry_at}.')
            output(status='cooldown')
            return
    config = load_config()
    identity, compute, network, block = clients(config)
    root = config['tenancy']
    if config['region'] != 'us-sanjose-1' or os.environ['OCI_COMPARTMENT_ID'] != root:
        raise ValueError('Unexpected region or compartment; no launch attempted.')
    subscriptions = identity.list_region_subscriptions(root).data
    if not any(r.is_home_region and r.region_name == config['region'] for r in subscriptions):
        raise ValueError('Target is not the home region; no launch attempted.')
    compartments = [root] + [c.id for c in all_items(
        identity.list_compartments, root, compartment_id_in_subtree=True,
        access_level='ACCESSIBLE') if c.lifecycle_state == 'ACTIVE']
    ads = identity.list_availability_domains(root).data
    if os.environ['AD_NAME'] not in [a.name for a in ads]:
        raise ValueError('Availability domain does not match account.')
    used_gb = 0
    for compartment in compartments:
        instances = all_items(compute.list_instances, compartment)
        servers = [i for i in instances if i.shape == ARM and i.lifecycle_state != 'TERMINATED']
        if servers:
            managed = [i for i in servers if i.freeform_tags.get('managed-by') == MARKER]
            summary('EXISTING: An ARM instance already exists. No new ARM instance will be created.'
                    + (' This workflow created it.' if managed else ' Review it in the OCI console.'))
            output(status='existing')
            return
        for ad in ads:
            used_gb += sum(v.size_in_gbs for v in all_items(
                block.list_boot_volumes, availability_domain=ad.name, compartment_id=compartment)
                if v.lifecycle_state != 'TERMINATED')
        used_gb += sum(v.size_in_gbs for v in all_items(
            block.list_volumes, compartment_id=compartment)
            if v.lifecycle_state != 'TERMINATED')
    if used_gb + boot_size() > 200:
        raise ValueError('Adding this boot volume would exceed the 200 GB free storage allowance.')
    subnet = network.get_subnet(os.environ['OCI_SUBNET_ID']).data
    if subnet.prohibit_public_ip_on_vnic or subnet.lifecycle_state != 'AVAILABLE':
        raise ValueError('Subnet is not an available public subnet.')
    image = compute.get_image(image_id()).data
    if image.operating_system != 'Canonical Ubuntu' or image.operating_system_version != '24.04':
        raise ValueError('Only Canonical Ubuntu 24.04 is allowed.')
    if image.compartment_id or image.launch_mode == 'EMULATED':
        raise ValueError('Only an Oracle platform image is allowed.')
    compatible = all_items(compute.list_image_shape_compatibility_entries, image.id)
    if SHAPE not in [entry.shape for entry in compatible]:
        raise ValueError('Image is not compatible with the selected free shape.')
    summary('Preflight passed: home region, no existing ARM server, public subnet, compatible official Ubuntu image, storage within 200 GB. Target: ARM 2 OCPU / 12 GB RAM / 100 GB boot volume. Existing AMD is retained.')
    output(status='ready')


def launch():
    config = load_config()
    compute = oci.core.ComputeClient(config, retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(15, 90))
    # Stable per repository. Never use automatic launch retries after uncertain transport errors.
    token = hashlib.sha256(('oci-free-proxy-v1:' + os.environ['GITHUB_REPOSITORY'] + ':' + SHAPE).encode()).hexdigest()
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=os.environ['OCI_COMPARTMENT_ID'],
        availability_domain=os.environ['AD_NAME'],
        display_name=NAME, shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id(), boot_volume_size_in_gbs=boot_size(), boot_volume_vpus_per_gb=10),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=os.environ['OCI_SUBNET_ID'], assign_public_ip=True),
        metadata={'ssh_authorized_keys': os.environ['SSH_PUBLIC_KEY']},
        freeform_tags={'managed-by': MARKER},
        is_pv_encryption_in_transit_enabled=True,
    )
    try:
        response = compute.launch_instance(details, opc_retry_token=token,
                                           retry_strategy=oci.retry.NoneRetryStrategy())
    except oci.exceptions.ServiceError as error:
        if error.status == 500 and 'out of host capacity' in error.message.lower():
            summary('CAPACITY_UNAVAILABLE: Oracle returned Out of host capacity. No instance accepted. The next scheduled run may retry.')
            output(status='capacity')
        elif error.status == 429 and error.code == 'TooManyRequests':
            retry_at = rate_limit_retry_at(error.headers)
            summary(f'RATE_LIMITED: Oracle rejected the request (429 TooManyRequests). No instance accepted. Cool down for at least 30 minutes; retry after Unix time {retry_at}.')
            output(status='rate_limited', retry_at=retry_at)
        else:
            summary(f'STOPPED: launch returned HTTP {error.status} ({error.code}). Review before any further request. No automatic retry.')
            output(status='uncertain')
        return
    except Exception as error:
        summary(f'UNCERTAIN: {type(error).__name__} while submitting launch. Stopping to prevent a duplicate. Check OCI before resuming.')
        output(status='uncertain')
        return
    if response.data.lifecycle_state in ('PROVISIONING', 'STARTING', 'RUNNING'):
        summary('ACCEPTED: Oracle accepted the instance. Automated attempts will stop now. Check OCI for RUNNING and its public IP; SSH connectivity has not yet been verified.')
        output(status='accepted')
    else:
        summary('UNCERTAIN: unexpected launch response. Stop and check OCI before resuming.')
        output(status='uncertain')


if __name__ == '__main__':
    try:
        {'check': check, 'launch': launch}[sys.argv[1]]()
    except Exception as error:
        # Avoid printing SDK exception bodies containing account/resource identifiers.
        summary(f'PRECHECK_FAILED: {type(error).__name__}. No launch request from this step. Check configuration and access.')
        raise SystemExit(1)
