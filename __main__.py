#!/usr/bin/env python
# encoding: utf-8


"""
Run the talus worker daemon, specifying the maximum number of cores to use,
the maximum RAM available for the VMs, the AMQP host to connect to, and
a plugins directory.
"""


# system imports
import argparse
import math
import multiprocessing
import os
import sys

# local imports
import slave


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        __file__,
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter
    )

    # see #28 - configurable RAM/cpus per VM
    total_ram = math.ceil(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024.0 ** 2))
    default_ram = total_ram - 1024 # leave one gb of ram left by default
    parser.add_argument("--ram",
        help="Maximum amount of ram to use in GB (default {})".format(
            default_ram
        ),
        type=int,
        required=False,
        default=default_ram/1024.0,
    )

    total_cpus = multiprocessing.cpu_count()  
    default_cpus = multiprocessing.cpu_count() - 2
    parser.add_argument("--cpus",
        help="Maximum number of cores to use (default {})".format(
            default_cpus
        ),
        type=int,
        required=False,
        default=default_cpus,
    )
    parser.add_argument("-i", "--intf",
        help="Network interface",
        type=str,
    )
    parser.add_argument("--plugins",
        type=str,
        help="Path to plugins directory"
    )
    parser.add_argument("--amqp",
        help="the hostname of the AMQP server",
        default=None,
    )

    args = parser.parse_args(sys.argv[1:])
    ram  = args.ram*1024
    if args.amqp is None:
        print("ERROR! --amqp must be specified")
        exit(1)
    # Two tests for user supplied ram and cpus to be < what the total possible amount is
    if ram > total_ram:
    print("ERROR! --ram must be less than total_ram")
    if args.cpus > total_cpus:
        print("ERROR! --cpu must be less than total_cpu")

    slave.main(
        amqp_host=args.amqp,
        max_ram=ram,
        max_cpus=args.cpus,
        intf=args.intf,
        plugins_dir=args.plugins
    )
