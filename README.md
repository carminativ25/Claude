# Juniper Switch Configuration

This repository contains a baseline configuration for Juniper EX series switches suitable for enterprise deployments.

## Configuration Overview

The `juniper-switch-config.conf` file includes:

### System Configuration
- Hostname: `juniper-switch-01`
- Domain: `example.com`
- SSH access (root login disabled)
- NETCONF over SSH
- Web management via HTTPS
- NTP servers for time synchronization
- Syslog configuration

### VLANs
- **VLAN 10** (vlan-data): Data network
- **VLAN 20** (vlan-voice): Voice/VoIP network
- **VLAN 30** (vlan-guest): Guest network
- **VLAN 99** (vlan-management): Management network
- **VLAN 1** (default): Default VLAN

### Interfaces
- **ge-0/0/0 - ge-0/0/1**: Access ports for Data VLAN (VLAN 10)
- **ge-0/0/2 - ge-0/0/3**: Access ports for Voice VLAN (VLAN 20)
- **ge-0/0/47**: Trunk port to core switch (carries all VLANs)
- **ae0**: Link Aggregation Group with LACP
- **ge-0/0/46**: Member of LAG ae0

### Protocols
- **RSTP**: Rapid Spanning Tree Protocol for loop prevention
- **LLDP**: Link Layer Discovery Protocol
- **LLDP-MED**: LLDP for Media Endpoint Devices

### Features
- Link Aggregation (802.3ad LACP)
- PoE (Power over Ethernet) enabled on all interfaces
- Storm control on all interfaces
- SNMP community strings (public/private)

## Deployment Instructions

1. **Review and Customize**:
   - Update the hostname and domain name
   - Set proper encrypted passwords for root and admin users
   - Adjust IP addresses for management interface
   - Modify VLAN IDs and names as needed
   - Update NTP servers to your preferred time sources

2. **Load Configuration**:
   ```
   configure
   load override juniper-switch-config.conf
   commit check
   commit and-quit
   ```

3. **Verify Configuration**:
   ```
   show configuration
   show vlans
   show interfaces terse
   show lacp interfaces
   show spanning-tree bridge
   ```

## Security Recommendations

- Change default passwords immediately
- Use strong encryption for password hashes
- Restrict SNMP community access or use SNMPv3
- Configure firewall filters as needed
- Implement port security (MAC limiting, DHCP snooping)
- Enable storm control thresholds appropriate for your environment

## Support

For Juniper JunOS documentation, visit: https://www.juniper.net/documentation/

## License

This configuration template is provided as-is for reference purposes.
