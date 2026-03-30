#!/bin/bash
#


sudo virsh destroy u26beta
sudo virsh snapshot-revert u26beta base_homeroot
ansible-playbook setup.yml
ansible-playbook btr.yml
