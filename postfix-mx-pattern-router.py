#!/usr/bin/env python3
"""
Postfix MX Pattern Router Service

This service acts as a TCP lookup table for Postfix to dynamically route emails based on
the MX records of the destination domain. It allows routing decisions to be made based on
pattern matching against MX hostnames.

Usage:
    python3 postfix-mx-pattern-router.py [options]

Options:
    -c, --config FILE    Path to configuration file (default: /etc/postfix/postfix-mx-pattern-router.conf)
    -p, --port PORT      Port to listen on (default: 10099)
    -H, --host HOST      Host to bind to (default: 127.0.0.1)

Configuration File Format:
    Each line should contain a pattern and a relay, separated by whitespace:
    pattern relay_transport

    Example:
    protection.outlook.com    relay:[office365-relay.example.com]:587
    mx.microsoft              relay:[office365-relay.example.com]:587
    icloud.com                relay:[icloud-relay.example.com]:587

Integration with Postfix:
    Add to /etc/postfix/main.cf:
    transport_maps = tcp:127.0.0.1:10099

    Then reload Postfix:
    systemctl reload postfix

Useful links:
 - https://www.postfix.org/transport.5.html
 - https://www.postfix.org/tcp_table.5.html
 - https://github.com/fbett/postfix-tcp-table-service
"""

import os
import sys
import socket
import re
import dns.resolver
import urllib.parse
import argparse

# Change to the script's directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Default values
DEFAULT_PORT = 10099
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PATTERN_FILE = '/etc/postfix/postfix-mx-pattern-router.conf'

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Postfix MX Pattern Router Service')
    parser.add_argument('-c', '--config',
                        default=DEFAULT_PATTERN_FILE,
                        help=f'Path to configuration file (default: {DEFAULT_PATTERN_FILE})')
    parser.add_argument('-p', '--port',
                        type=int,
                        default=DEFAULT_PORT,
                        help=f'Port to listen on (default: {DEFAULT_PORT})')
    parser.add_argument('-H', '--host',
                        default=DEFAULT_HOST,
                        help=f'Host to bind to (default: {DEFAULT_HOST})')
    return parser.parse_args()

def load_patterns(file_path):
    """Load MX patterns from configuration file."""
    patterns = {}
    if not os.path.exists(file_path):
        sys.stderr.write(f"Pattern file not found: {file_path}\n")
        return patterns

    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = re.split(r'\s+', line, 1)
            if len(parts) == 2:
                patterns[parts[0].lower()] = parts[1]

    return patterns

def get_mx_records(domain):
    """Get MX records for a domain using dns.resolver."""
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        return [answer.exchange.to_text().rstrip('.').lower() for answer in answers]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []

def main():
    # Parse command line arguments
    args = parse_arguments()

    # Load patterns from the specified configuration file
    patterns = load_patterns(args.config)
    if not patterns:
        sys.stderr.write(f"Warning: No patterns loaded from {args.config}\n")

    # Create socket server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((args.host, args.port))
        server.listen(5)
        sys.stdout.write(f"Socketmap server listening on {args.host}:{args.port}\n")
        sys.stdout.flush()

        while True:
            conn, addr = server.accept()
            try:
                data = conn.recv(1024).decode('utf-8').strip()

                matched = False
                # Match 'get email@domain'
                if data != 'get *':
                    email_match = re.match(r'^get\s+([\w.+-]+@[\w.-]+)$', data, re.IGNORECASE)
                    if email_match:
                        email = email_match.group(1).lower()
                        parts = email.split('@')
                        if len(parts) == 2:
                            domain = parts[1]
                            mx_records = get_mx_records(domain)

                            for mx in mx_records:
                                for pattern, relay in patterns.items():
                                    if pattern in mx:
                                        matched = relay
                                        break
                                if matched:
                                    break

                if matched:
                    response = f"200 {urllib.parse.quote(matched)}\n"
                    conn.sendall(response.encode('utf-8'))
                    sys.stdout.write(f"Matched: {domain} → {matched}\n")
                    sys.stdout.flush()
                else:
                    conn.sendall(b"500 NO%20RESULT\n")

            except Exception as e:
                sys.stderr.write(f"Error handling connection: {e}\n")
            finally:
                conn.close()

    except Exception as e:
        sys.stderr.write(f"Failed to start server: {e}\n")
        sys.exit(1)
    finally:
        server.close()

if __name__ == "__main__":
    main()
