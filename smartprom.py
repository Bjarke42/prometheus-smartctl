#!/usr/bin/env python3
import json
import os
import subprocess
import time
import argparse
import re
from typing import Tuple

import prometheus_client

#LABELS = ['drive', 'type', 'model_family', 'model_name', 'serial_number','revision','capacity']
LABELS = ['drive', 'type', 'model_family', 'model_name', 'serial_number','revision','capacity','mpath','zpool']
DRIVES = {}
METRICS = {}

# https://www.smartmontools.org/wiki/USB
SAT_TYPES = ['sat', 'usbjmicron', 'usbprolific', 'usbsunplus']
NVME_TYPES = ['nvme', 'sntasmedia', 'sntjmicron', 'sntrealtek']
SCSI_TYPES = ['scsi']


def run_smartctl_cmd(args: list) -> Tuple[str, int]:
    """
    Runs the smartctl command on the system
    """
    out = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, stderr = out.communicate()

    # exit code can be != 0 even if the command returned valid data
    # see EXIT STATUS in
    # https://www.smartmontools.org/browser/trunk/smartmontools/smartctl.8.in
    if out.returncode != 0:
        stdout_msg = stdout.decode('utf-8') if stdout is not None else ''
        stderr_msg = stderr.decode('utf-8') if stderr is not None else ''
        print(f"WARNING: Command returned exit code {out.returncode}. Stdout: '{stdout_msg}' Stderr: '{stderr_msg}'")

    return stdout.decode("utf-8"), out.returncode


def get_drives() -> dict:
    """
    Returns a dictionary of devices and its types
    """
    disks = {}
    if alt_smartctl is None:
        result, _ = run_smartctl_cmd(['smartctl', '--scan-open', '--json=c'])
    else:
        result, _ = run_smartctl_cmd([alt_smartctl, '--scan-open', '--json=c'])
    result_json = json.loads(result)
    if 'devices' in result_json:
        devices = result_json['devices']
        if connect_mpath_with_zpool == True:
                zpools = parse_zpool_list()
        for device in devices:
            dev = device["name"]
            typ = device["type"]
            if device.get("open_error","None") != "None": #cannot use device with open_error
                print(f"Warning device {dev} with type {typ} has an open_error, will be skipped!")
                continue
            if typ not in SAT_TYPES and typ not in NVME_TYPES and typ not in SCSI_TYPES: # remove drive types we cannot handle
                print(f"Warning device {dev} with type {typ} is not a valid, will be skipped!")
                continue
            disk_attrs = get_device_info(dev)
            if show_mpath == True:
                disk_attrs['mpath']=get_mpath_info(dev)
                if connect_mpath_with_zpool == True:
                    disk_attrs['zpool']=zpools.get(disk_attrs['mpath'],"None")  #match disk to disk
                    if connect_mpath_to_part == True and disk_attrs['zpool']=="None": #match disk to partition - dirty,dirty....
                        res=get_mpath_part_info(dev)
                        rt=res.get('children',"None")
                        if rt != "None":  # if there are any children(partitions) returned from lsblk
                            rex_pattern=disk_attrs['mpath'] + "*"
                            pattern=re.compile(rex_pattern)
                            already_matched=0
                            matched_str=""
                            matched_total=""
                            for ri in res['children']:
                                if pattern.match(ri.get('name')) and already_matched>0:
                                    matched_str=zpools.get(ri.get('name'),"None")
                                    if matched_str != "None":
                                        already_matched=2
                                        matched_total=matched_total + ", " + zpools.get(ri.get('name'),"None")
                                if pattern.match(ri.get('name')) and already_matched==0:
                                    matched_str=zpools.get(ri.get('name'),"None")
                                    if matched_str != "None":
                                        already_matched=1
                                        matched_total=zpools.get(ri.get('name'),"None")
                            if already_matched>0:
                                disk_attrs['zpool']=matched_total
            disk_attrs["type"] = device["type"]
            disks[dev] = disk_attrs
            print("Discovered device", dev, "with attributes", disk_attrs)
    else:
        print("No devices found. Make sure you have enough privileges.")
    return disks


def get_device_info(dev: str) -> dict:
    """
    Returns a dictionary of device info
    """
    results, _ = run_smartctl_cmd(['smartctl', '-i', '--json=c', dev])
    results = json.loads(results)
    return {
        'model_family': results.get("model_family", "Unknown"),
        'model_name': results.get("model_name", "Unknown"),
        'serial_number': results.get("serial_number", "Unknown"),
        'revision': results.get("revision", "Unknown"),
        'capacity': results.get('user_capacity',{}).get('bytes','Unknown')
    }

def get_mpath_info(dev: str):
    """
    Returns mpath info on a device from lsblk
    """
    result, _ = run_smartctl_cmd(['lsblk', '--json', dev])
    result = json.loads(result)
    return result['blockdevices'][0]['children'][0]['name']

def get_mpath_part_info(dev: str):
    """
    Returns mpath children on a defice from lsblk. This should also contain partitions on a disk
    """
    result, _ = run_smartctl_cmd(['lsblk', '--json', dev])
    result = json.loads(result)
    return result['blockdevices'][0]['children'][0]


def parse_zpool_list() -> dict:
    """
    Returns a dictionary with mpath device as key and what zpool it belongs to in value.
    """
    results, _ = run_smartctl_cmd(['zpool', 'list', '-v', '-H'])
    state=0
    counter=0 #used to identify the mirror name in parsing
    disk_counter=0 #counts regular disks in a pool
    cdisk_counter=0 # counts cache disks in a pool
    #zpools=[]
    #zdisks=[]
    #zdisks_pr_pool=[]
    #zcdisks=[]
    #zcdisks_pr_pool=[]
    zpool_current_pool=""
    zpool_dict={}
    for line in results.splitlines():
        if state==0 and ord(line[0])==9: # 9 is tab 
            state=1
        if state==1 and counter>1:
            state=2
        if state==2 and line.split('\t')[0].split(' ')[0]=='cache':
            state=3
        if state==3 and ord(line[0])==9:
            state=4
            #zdisks_pr_pool.append(disk_counter)
        if state==4 and ord(line[0])!=9: #reset back to state 0
            state=0
            counter=0
            disk_counter=0
            #if cdisk_counter>0:
            #    zcdisks_pr_pool.append(cdisk_counter)
            cdisk_counter=0
        if state==0:    # name of zpool
            #zpools.append(line.split('\t')[0])
            zpool_current_pool=line.split('\t')[0]
            counter=counter+1
        if state==1:    # mirror
            counter=counter+1
        if state==2:    # disks in zpool
            #print(state,disk_counter,line)
            #zdisks.append(line.split('\t')[1].split(' ')[0])
            zpool_dict[line.split('\t')[1].split(' ')[0]]=zpool_current_pool
            disk_counter=disk_counter+1
        #else:
            #print(state,line)
        if state==4:    # cache disks
            #zcdisks.append(line.split('\t')[1].split(' ')[0])
            zpool_dict[line.split('\t')[1].split(' ')[0]]=zpool_current_pool
            cdisk_counter=cdisk_counter+1
    return zpool_dict

def get_smart_status(results: dict) -> int:
    """
    Returns a 1, 0 or -1 depending on if result from
    smart status is True, False or unknown.
    """
    status = results.get("smart_status")
    return +(status.get("passed")) if status is not None else -1


def smart_sat(dev: str) -> dict:
    """
    Runs the smartctl command on a internal or external "sat" device
    and processes its attributes
    """
    results, exit_code = run_smartctl_cmd(['smartctl', '-A', '-H', '-d', 'sat', '--json=c', dev])
    results = json.loads(results)

    attributes = {
        'smart_passed': (0, get_smart_status(results)),
        'exit_code': (0, exit_code)
    }
    data = results['ata_smart_attributes']['table']
    for metric in data:
        code = metric['id']
        name = metric['name']
        value = metric['value']

        # metric['raw']['value'] contains values difficult to understand for temperatures and time up
        # that's why we added some logic to parse the string value
        value_raw = metric['raw']['string']
        try:
            # example value_raw: "33" or "43 (Min/Max 39/46)"
            value_raw = int(value_raw.split()[0])
        except:
            # example value_raw: "20071h+27m+15.375s"
            if 'h+' in value_raw:
                value_raw = int(value_raw.split('h+')[0])
            else:
                print(f"Raw value of sat metric '{name}' can't be parsed. raw_string: {value_raw} "
                      f"raw_int: {metric['raw']['value']}")
                value_raw = None

        attributes[name] = (int(code), value)
        if value_raw is not None:
            attributes[f'{name}_raw'] = (int(code), value_raw)
    return attributes


def smart_nvme(dev: str) -> dict:
    """
    Runs the smartctl command on a internal or external "nvme" device
    and processes its attributes
    """
    results, exit_code = run_smartctl_cmd(['smartctl', '-A', '-H', '-d', 'nvme', '--json=c', dev])
    results = json.loads(results)

    attributes = {
        'smart_passed': get_smart_status(results),
        'exit_code': exit_code
    }
    data = results['nvme_smart_health_information_log']
    for key, value in data.items():
        if key == 'temperature_sensors':
            for i, _value in enumerate(value, start=1):
                attributes[f'temperature_sensor{i}'] = _value
        else:
            attributes[key] = value
    return attributes


def smart_scsi(dev: str) -> dict:
    """
    Runs the smartctl command on a "scsi" device
    and processes its attributes
    """
    results, exit_code = run_smartctl_cmd(['smartctl', '-A', '-H', '-d', 'scsi', '--json=c', dev])
    results = json.loads(results)

    attributes = {
        'smart_passed': get_smart_status(results),
        'exit_code': exit_code
    }
    for key, value in results.items():
        if type(value) == dict:
            for _label, _value in value.items():
                if type(_value) == int:
                    attributes[f"{key}_{_label}"] = _value
        elif type(value) == int:
            attributes[key] = value
    return attributes


def collect():
    """
    Collect all drive metrics and save them as Gauge type
    """
    global LABELS, DRIVES, METRICS, SAT_TYPES, NVME_TYPES, SCSI_TYPES

    for drive, drive_attrs in DRIVES.items():
        typ = drive_attrs['type']
        try:
            if typ in SAT_TYPES and ignore_sata:
                attrs = smart_sat(drive)
            elif typ in NVME_TYPES:
                attrs = smart_nvme(drive)
            elif typ in SCSI_TYPES:
                attrs = smart_scsi(drive)
            else:
                continue

            for key, values in attrs.items():
                # Metric name in lower case
                metric = 'smartprom_' + key.replace('-', '_').replace(' ', '_').replace('.', '').replace('/', '_') \
                    .lower()

                # Create metric if it does not exist
                if metric not in METRICS:
                    desc = key.replace('_', ' ')
                    code = hex(values[0]) if typ in SAT_TYPES else hex(values)
                    print(f'Adding new gauge {metric} ({code})')
                    METRICS[metric] = prometheus_client.Gauge(metric, f'({code}) {desc}', LABELS)

                # Update metric
                metric_val = values[1] if typ in SAT_TYPES else values

                METRICS[metric].labels(drive=drive,
                                       type=typ,
                                       model_family=drive_attrs['model_family'],
                                       model_name=drive_attrs['model_name'],
                                       serial_number=drive_attrs['serial_number'],
                                       revision=drive_attrs['revision'],
                                       capacity=drive_attrs['capacity'],
                                       mpath=drive_attrs['mpath'],
                                       zpool=drive_attrs['zpool']
                                       ).set(metric_val)
        except Exception as e:
            print('Exception:', e)
            pass


def main():
    """
    Starts a server and exposes the metrics
    """
    global DRIVES
    global ignore_sata
    global show_mpath
    global connect_mpath_with_zpool
    global connect_mpath_to_part
    global alt_smartctl

    # Validate configuration
    exporter_address = os.environ.get("SMARTCTL_EXPORTER_ADDRESS", "0.0.0.0")
    exporter_port = int(os.environ.get("SMARTCTL_EXPORTER_PORT", 9902))
    refresh_interval = int(os.environ.get("SMARTCTL_REFRESH_INTERVAL", 60))

    # Get program arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--web.listen-address",help="Address to bind to",type=str,required=False,dest="address",metavar="0.0.0.0")
    parser.add_argument("--web.listen-port",help="Port to listen",type=int,required=False,dest="port",metavar=9633)
    parser.add_argument("--smartctl.interval",help="Update interval for data",type=int,required=False,dest="interval",metavar=60)
    parser.add_argument("--ignore_sata",help="Do not collect data about sata devices",const=True,required=False,dest="ignore_sata",action="store_const")
    parser.add_argument("--show_mpath",help="Connect disk device with multipath device name",const=True,required=False,dest="show_mpath",action="store_const")
    parser.add_argument("--connect_mpath_with_zpool",help="Connect multipath device name to ZFS zpool",const=True,required=False,dest="connect_mpath_with_zpool",action="store_const")
    parser.add_argument("--connect_mpath_to_part",help="Connect multipath device name to ZFS zpool that uses a parittion",const=True,required=False,dest="connect_mpath_to_part",action="store_const")
    parser.add_argument("--smartctl",help="Path to alternative smartctl",type=str,required=False,dest="alt_smartctl",metavar="/home/usr/smartctl")
    
    args=parser.parse_args()

    if args.address is not None:
        exporter_address = args.address
    if args.port is not None:
        exporter_port= args.port
    if args.interval is not None:
        refresh_interval = args.interval

    if args.ignore_sata:
        ignore_sata = False
    else:
        ignore_sata = True
    
    if args.show_mpath:
        show_mpath = True
    else:
        show_mpath = False

    if args.connect_mpath_with_zpool:
        connect_mpath_with_zpool = True
    else:
        connect_mpath_with_zpool = False
    
    if args.connect_mpath_to_part:
        connect_mpath_to_part = True
    else:
        connect_mpath_to_part = False
    
    alt_smartctl = None

    if args.alt_smartctl is not None:
        alt_smartctl = args.alt_smartctl

    # Get drives (test smartctl)
    DRIVES = get_drives()

    # Start Prometheus server
    prometheus_client.start_http_server(exporter_port, exporter_address)
    print(f"Server listening in http://{exporter_address}:{exporter_port}/metrics")

    while True:
        collect()
        time.sleep(refresh_interval)


if __name__ == '__main__':
    main()
