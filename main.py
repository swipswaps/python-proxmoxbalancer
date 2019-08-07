#
# Proxmox Balance script.
#
# Author: Skylar Kelty
#

import sys
import yaml
import argparse
import operator
from proxmoxer import ProxmoxAPI

class ProxmoxBalance:
    vm_list = []
    config = {}
    node_list = {}
    dry = False
    proxmox = False

    def __init__(self):
        # Read args.
        parser = argparse.ArgumentParser()
        parser.add_argument('-d', '--dry', action='store_true')
        args = parser.parse_args()
        self.dry = args.dry

        # Read config, sanitize, fire up the API.
        with open('config.yaml', 'r') as stream:
            try:
                config = yaml.safe_load(stream)
                if 'method' not in config:
                    config['method'] = 'current'
                if 'allowed_disparity' not in config:
                    config['allowed_disparity'] = 20
                if 'rules' not in config:
                    config['rules'] = {}
                if 'separate' not in config['rules']:
                    config['rules']['separate'] = {}
            except yaml.YAMLError as exc:
                print(exc)
                sys.exit(1)

        self.config = config
        self.proxmox = ProxmoxAPI(config['host'], user=config['user'], password=config['password'], backend='https', verify_ssl=False)

    # Get various useful sum.
    def get_totals(self):
        total_disparity = 0
        total_nodes = len(self.node_list)
        total_points = sum([self.node_list[node]['points'] for node in self.node_list])
        total_used_points = sum([self.node_list[node]['used_points'] for node in self.node_list])
        avg_points = (total_used_points / total_nodes) + 0.0
        return (total_disparity, total_nodes, total_points, total_used_points, avg_points)

    # Calculate the overall imbalance in the cluster, this can be useful for
    # determining if we should even run Balance.
    def calculate_imbalance(self):
        # Work out total imbalance as a percentage
        (total_disparity, total_nodes, total_points, total_used_points, avg_points) = self.get_totals()
        for node in self.node_list:
            points = self.node_list[node]['used_points']
            total_disparity += abs(avg_points - points)
            disparity = abs(100 - ((points / avg_points) * 100))
            if disparity > 30:
                print("Found imbalance in node %s (%i" % (node, disparity) + '%)')

        return total_disparity

    # Work out the best host for a given VM.
    def calculate_best_host(self, current_node, vm_name, points, separate):
        (total_disparity, total_nodes, total_points, total_used_points, avg_points) = self.get_totals()
        new_host = False
        new_host_points = 0
        for node_name in self.node_list:
            if node_name == current_node:
                continue

            # Make sure we abide by the rules.
            skip = False
            for rule in separate:
                if vm_name in rule:
                    for vm in rule:
                        if vm != vm_name and vm in self.node_list[node_name]['vms']:
                            skip = True
            if skip:
                continue

            # This is not particularly forward-thinking but it will do for now.
            new_points = self.node_list[node_name]['used_points'] + points
            if new_points < self.node_list[current_node]['used_points'] and (new_points < new_host_points or new_host_points == 0):
                new_host = node_name
                new_host_points = new_points
        return new_host

    # Runs a balance pass over the node list.
    def balance_pass(self):
        operations = []
        rules = self.config['rules']

        # Order vm_list.
        self.vm_list.sort(key=operator.itemgetter('points'))
        self.vm_list.reverse()

        # List of vms to keep separate.
        separate = [rule.split(',') for rule in rules['separate']]

        # Loop through every VM, if we find one that we can migrate to another host without
        # making that hosts' total points greater than our own, do that.
        for node_name in self.node_list:
            for vm_name in self.node_list[node_name]['vms']:
                points = self.node_list[node_name]['vms'][vm_name]['points']
                target = self.calculate_best_host(node_name, vm_name, points, separate)
                if target:
                    operations.append({
                        'vm_name': vm_name,
                        'host': node_name,
                        'target': target
                    })

                    self.node_list[node_name]['used_points'] -= points
                    self.node_list[target]['used_points'] += points

        return operations

    # Actually migrate a VM.
    def run_migrate(self, operation):
        vm_name = operation['vm_name']
        host = operation['host']
        target = operation['target']
        vmid = self.node_list[host]['vms'][vm_name]['vmid']
        data = {
            'target': target,
            'online': 1,
        }
        print("Moving %s from %s to %s" % (vm_name, host, target))
        if not self.dry:
            self.proxmox.nodes(host).qemu(vmid).migrate.post(**data)

    # Pretty print the points used.
    def pretty_print_points(self):
        for name in self.node_list:
            node = self.node_list[name]
            print("Found host %s with %i points (%i used)." % (name, node['points'], node['used_points']))

    # Calculate points for a given VM.
    # We're going to assign points to each server and VM based on CPU/RAM requirements.
    # Each CPU core is worth 5 points, each GB ram is 1 point.
    def calculate_vm_points(self, vm):
        if self.config['method'] == 'max':
            return (vm['maxcpu'] * 5) + ((vm['maxmem'] / 1024 / 1024 / 1024) * 1)
        return (vm['cpu'] * 5) + ((vm['mem'] / 1024 / 1024 / 1024) * 1)

    # Generate node_list and vm_list.
    def regenerate_lists(self):
        for node in self.proxmox.nodes.get():
            node_name = node['node']
            self.node_list[node_name] = node
            self.node_list[node_name]['vms'] = {}

            # Calculate points.
            points = (node['maxcpu'] * 5) + ((node['maxmem'] / 1024 / 1024 / 1024) * 1)
            self.node_list[node_name]['points'] = points
            self.node_list[node_name]['used_points'] = 0

            for vm in self.proxmox.nodes(node_name).qemu.get():
                vm_name = vm['name']
                if vm['status'] == 'running':
                    points = self.calculate_vm_points(vm)
                    self.node_list[node_name]['vms'][vm_name] = vm
                    self.node_list[node_name]['vms'][vm_name]['points'] = points
                    self.node_list[node_name]['used_points'] += points
                    self.vm_list.append({
                        'obj': vm,
                        'node': node_name,
                        'points': points,
                    })


    def balance(self):
        # First get the current list of hosts and VMs.
        self.regenerate_lists()

        # Okay, work out the imbalance here and run migrations.
        total_disparity = self.calculate_imbalance()
        if total_disparity > (len(self.node_list) * self.config['allowed_disparity']):
            print("Running Balance%s..." % (' (dry mode)' if self.dry else ''))

            # Now, we need to spread the load.
            # We're going to work out how to best spread out with the minimal number of migrations.
            self.pretty_print_points()

            # Okay, this is not optimal. When we get more than the hour I've given myself for this we
            # can use some fancy balancing graph, but for now, we will just move a few things to try and balance it.
            operations = self.balance_pass()
            for operation in operations:
                self.run_migrate(operation)

            # Now, we need to spread the load.
            # We're going to work out how to best spread out with the minimal number of migrations.
            self.pretty_print_points()
        else:
            print('Acceptable overall imbalance, not running balance.')

balancer = ProxmoxBalance()
balancer.balance()