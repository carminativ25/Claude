# Juniper EX2300-48P Client Isolation Configuration Guide

## Overview

This configuration implements **Private VLANs** on a Juniper EX2300-48P switch to create a client network where all devices are isolated from each other but can still communicate with the gateway/router.

## How It Works

### Private VLAN Architecture

The configuration uses a **Primary VLAN** and an **Isolated VLAN**:

- **Primary VLAN (100)**: `clients-primary`
  - Configured on uplink ports (ge-0/0/46 and ge-0/0/47)
  - These are "promiscuous" ports that can communicate with all isolated clients
  - Typically connected to your router/gateway

- **Isolated VLAN (101)**: `clients-isolated`
  - Configured on all client access ports (ge-0/0/0 through ge-0/0/45)
  - Devices on these ports **CANNOT** communicate with each other
  - Devices **CAN** communicate with promiscuous ports (uplinks)

### Traffic Flow

```
Client A (ge-0/0/0) ──X──> Client B (ge-0/0/1)    ❌ BLOCKED
Client A (ge-0/0/0) ────> Gateway (ge-0/0/47)    ✅ ALLOWED
Client B (ge-0/0/1) ────> Gateway (ge-0/0/47)    ✅ ALLOWED
Gateway (ge-0/0/47) ────> Client A (ge-0/0/0)    ✅ ALLOWED
```

## Port Assignments

| Port Range | Type | Purpose |
|------------|------|---------|
| ge-0/0/0 - ge-0/0/45 | Isolated Access | Client devices (46 ports) |
| ge-0/0/46 | Promiscuous Trunk | Secondary uplink to gateway |
| ge-0/0/47 | Promiscuous Trunk | Primary uplink to gateway |

## Allowing Specific Devices to Communicate

If you need specific devices to communicate with each other, you have several options:

### Option 1: Community VLAN (Recommended)

Create a community VLAN where specific ports can talk to each other:

```
vlans {
    clients-primary {
        vlan-id 100;
        interface {
            ge-0/0/46.0;
            ge-0/0/47.0;
        }
        isolated-vlan 101;
        community-vlan 102;  ## Add this
    }

    clients-isolated {
        vlan-id 101;
        isolated-vlan;
    }

    clients-community {
        description "Community VLAN - These devices can talk to each other";
        vlan-id 102;
        community-vlan;
    }
}
```

Then assign specific ports to the community VLAN:

```
interfaces {
    ge-0/0/10 {
        description "Server 1 - Can talk to other community members";
        unit 0 {
            family ethernet-switching {
                interface-mode access;
                vlan {
                    members clients-community;
                }
            }
        }
    }

    ge-0/0/11 {
        description "Server 2 - Can talk to other community members";
        unit 0 {
            family ethernet-switching {
                interface-mode access;
                vlan {
                    members clients-community;
                }
            }
        }
    }
}
```

### Option 2: Move to Standard VLAN

For specific ports that need full communication, move them to a standard VLAN:

```
vlans {
    servers {
        description "Server VLAN - Full communication";
        vlan-id 50;
    }
}

interfaces {
    ge-0/0/10 {
        unit 0 {
            family ethernet-switching {
                interface-mode access;
                vlan {
                    members servers;
                }
            }
        }
    }
}
```

### Option 3: Firewall Filters

Use firewall filters to selectively allow traffic between specific isolated ports based on IP addresses:

```
firewall {
    family ethernet-switching {
        filter allow-printer {
            term allow-printer-ip {
                from {
                    source-address {
                        192.168.100.50/32;  ## Printer IP
                    }
                }
                then accept;
            }
            term default {
                then accept;
            }
        }
    }
}

interfaces {
    ge-0/0/5 {
        description "Client that can access printer";
        unit 0 {
            family ethernet-switching {
                filter {
                    input allow-printer;
                }
                vlan {
                    members clients-isolated;
                }
            }
        }
    }
}
```

## Deployment Instructions

### 1. Review and Customize

Before deploying:
- Set proper encrypted passwords for root and admin users
- Adjust management IP address (currently 192.168.1.100/24)
- Verify uplink ports (ge-0/0/46, ge-0/0/47) match your topology
- Update NTP servers if needed

### 2. Load Configuration

```bash
configure
load override juniper-ex2300-48p-isolated.conf
commit check
commit confirmed 5  ## Auto-rollback in 5 minutes if not confirmed
```

Test connectivity, then confirm:
```bash
commit
```

### 3. Verify Configuration

```bash
## Check VLAN configuration
show vlans

## Verify private VLAN setup
show vlans detail

## Check interface assignments
show interfaces terse | match ge-0/0

## View ethernet switching table
show ethernet-switching table

## Check PoE status (all 48 ports support PoE+)
show poe interface
```

### 4. Test Isolation

From the gateway/router, ping two client devices to verify:
- Clients can reach the gateway ✅
- Clients cannot ping each other ❌

## Security Enhancements

Consider adding these security features:

### MAC Limiting
```
interfaces {
    ge-0/0/0 {
        unit 0 {
            family ethernet-switching {
                port-security {
                    mac-limit 2;  ## Max 2 MAC addresses per port
                }
            }
        }
    }
}
```

### DHCP Snooping
```
vlans {
    clients-primary {
        forwarding-options {
            dhcp-security {
                group client-group {
                    interface ge-0/0/47.0 {
                        trusted;  ## Uplink with DHCP server
                    }
                }
            }
        }
    }
}
```

### Dynamic ARP Inspection (DAI)
```
vlans {
    clients-primary {
        forwarding-options {
            dhcp-security {
                arp-inspection;
            }
        }
    }
}
```

## Troubleshooting

### Clients cannot reach gateway
```bash
show vlans clients-primary
show vlans clients-isolated
show interfaces ge-0/0/47  ## Check uplink
```

### Need to temporarily disable isolation for testing
```bash
configure
deactivate vlans clients-isolated isolated-vlan
commit
## Test...
activate vlans clients-isolated isolated-vlan
commit
```

### Check which ports are in which VLAN
```bash
show ethernet-switching interfaces detail
```

## Use Cases

This configuration is ideal for:
- **Guest networks**: Isolate guest devices from each other
- **Public WiFi**: Prevent clients from seeing each other
- **Hotel networks**: Each room isolated
- **Student labs**: Prevent lateral movement between workstations
- **IoT devices**: Isolate smart devices while allowing internet access
- **Security**: Prevent lateral movement in case of device compromise

## Performance

The EX2300-48P specifications:
- **Switching capacity**: 128 Gbps
- **Throughput**: 95.2 Mpps
- **PoE+ power budget**: 740W (up to 30W per port)
- **Latency**: < 5 microseconds

Private VLANs have negligible performance impact on this platform.

## Support

For Juniper EX2300 documentation:
- [EX2300 Configuration Guide](https://www.juniper.net/documentation/us/en/software/junos/multicast-l2/topics/topic-map/layer-2-understanding-private-vlans.html)
- [Private VLAN Configuration](https://www.juniper.net/documentation/us/en/software/junos/multicast-l2/topics/topic-map/layer-2-configuring-private-vlans.html)
