# Postfix MX Smart Router Service

A fork of [postfix-mx-pattern-router](https://github.com/filidorwiese/postfix-mx-pattern-router) which implements **Weighted Round Robin**
but is incompatible with the original configuration.


This fork makes substantial changes to the original project by Filidor Wiese:

- support for Weighted Round Robin mx server groups
- each rule can target a specific group
- all servers are used if no group is chosen by a rule and no default rule is set
- server groups have the same percentage usage as the main list. 
  keep this into consideration when choosing the percentage for the individual servers
- New configuration in yaml
    - server perc is the percentage out of 100 that this server should be chosen when a 
      mail targets that group and an mx address is returned
    - default allows you to specify a default group; otherwise all servers are used
    - copy `postfix-mx-smart-router.yaml.example` to `postfix-mx-smart-router.yaml`, edit your server groups and pattern rules

- on CTRL-C exit gracefully and show some stats such as : 
```
    Group good
    Name          # Sent |  curr. % / target %
        mx1              5 |  41.6667 /  40.0000
        mx2              5 |  41.6667 /  40.0000
        mx3              2 |  16.6667 /  20.0000

    Group bad
    Name          # Sent |  curr. % / target %
        mx4              1 | 100.0000 /  32.2581
        mx5              0 |   0.0000 /   3.2258
        mx6              0 |   0.0000 /  32.2581
        mx7              0 |   0.0000 /  32.2581
```

## Installation
To quickly set it up, after checking out the code, 
- create a virtual environment in `.venv` and activate it
- installport requirements
- copy `postfix-mx-smart-router.yaml.example` to `postfix-mx-smart-router.yaml`, edit your server groups and pattern rules
- run the service for testing

```bash
    $ python -m venv .venv
    $ . .venv/bin/activate
    $ .venv/bin/pip install -r requirements.txt
    $ .venv/bin/python ./postfix-mx-smart-router.py -v
```

- query the service with

```bash
    $ echo "get xyz@gmail.com" | nc 127.0.0.1 10099
```

## Expected response
The service responds with:
- 200 followed by the URL-encoded relay server *from the selected group* if a match is found
- 200 followed by the URL-encoded relay server *from the default group/all servers* if **no** match is found
- 500 NO%20RESULT is never returned

## End of updated part
Please find the original README below, as it appeared at the time of this fork October 3rd, 2025; most of it is still valid, 
The only notable difference is the different name: `postfix-mx-smart-router.py` and **different configuration** filename, format and options


# Postfix MX Pattern Router Service

This service acts as a TCP lookup table for Postfix to dynamically route emails based on
the MX records of the destination domain. It allows routing decisions to be made based on
pattern matching against MX hostnames.

## Operation

When Postfix needs to deliver an email, it queries this service with the destination domain. The service:

1. Looks up the domain's MX records
2. Compares them against the defined patterns in the configuration file
3. If a match is found, it returns the corresponding relay server
4. If no match is found, Postfix will use its default transport (usually direct delivery)

This can be useful to, for example, optimize email delivery for domains that use the Microsoft mail infrastructure by routing these emails through specialized third-party SMTP relays with established sender reputations.

### Pattern Matching Behavior

The service uses substring matching for MX patterns, not exact matching. This means:

- Patterns like `protection.outlook.com` will match MX records such as `hotmail-com.olc.protection.outlook.com`
- You can use shorter, more generic patterns to match multiple similar MX records
- The first pattern that matches any part of an MX record will be used
- Patterns are checked in the order they appear in the configuration file

**Please be aware that patterns are not matched against recipient domain but the MX records of that domain!**

## Installation

### Requirements

- Python 3.6 or higher

### Setup

1. Clone this repository:

```bash
$ git clone https://github.com/filidorwiese/postfix-mx-pattern-router.git /usr/local/bin/postfix-mx-pattern-router
$ cd /usr/local/bin/postfix-mx-pattern-router
```

2. Install dependencies:

```bash
$ pip install -r requirements.txt
```

Or use package manager from your distribution.

3. Create the configuration file to define your MX patterns:

```bash
$ nano /etc/postfix/postfix-mx-pattern-router.conf
```

Example configuration:
```
protection.outlook.com    relay:[office365-relay.example.com]:587
mx.microsoft              relay:[office365-relay.example.com]:587
icloud.com                relay:[icloud-relay.example.com]:587
```

## Running as a Service

### Create a Dedicated System User

For security reasons, it's recommended to run the service under a dedicated system user with minimal privileges:

```bash
# Create a system user and group without login capabilities
$ groupadd --system postfix-mx-pattern-router
$ useradd --system --no-create-home --shell /usr/sbin/nologin -g postfix-mx-pattern-router postfix-mx-pattern-router
```

### Systemd Service

Create a systemd unit file to run the service as a daemon:

```bash
$ nano /etc/systemd/system/postfix-mx-pattern-router.service
```

Add the following content:

```ini
[Unit]
Description=Postfix MX Pattern Router Service
After=network.target

[Service]
ExecStart=/usr/local/bin/postfix-mx-pattern-router/postfix-mx-pattern-router.py -c /etc/postfix/postfix-mx-pattern-router.conf -p 10099 --cache-ttl 3600
Restart=on-failure
User=postfix-mx-pattern-router
Group=postfix-mx-pattern-router
StandardOutput=journal
StandardError=journal
SyslogIdentifier=postfix-mx-pattern-router
SyslogFacility=mail

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
$ systemctl enable postfix-mx-pattern-router
$ systemctl start postfix-mx-pattern-router
```

Check the status:

```bash
$ systemctl status postfix-mx-pattern-router
```

## Integration with Postfix

Add the following to your Postfix configuration (`/etc/postfix/main.cf`):

```
transport_maps = tcp:[127.0.0.1]:10099
```

Then reload Postfix.

## Testing the Service

You can test the service directly from the command line using netcat (nc) to simulate Postfix queries:

```bash
$ echo "get user@outlook.com" | nc 127.0.0.1 10099
200 relay%3A%5Boffice365-relay.example.com%5D%3A587
```
```bash
$ echo "get user@gmail.com" | nc 127.0.0.1 10099
500 NO%20RESULT
```

The service responds with:
- 200 followed by the URL-encoded relay server if a match is found
- 500 NO%20RESULT if no match is found

You can also check the logs for more detailed information:

```bash
$ journalctl -u postfix-mx-pattern-router -f
```

## License
This project is licensed under the BSD 3-Clause License - see the LICENSE file for details.

https://github.com/filidorwiese/postfix-mx-pattern-router
