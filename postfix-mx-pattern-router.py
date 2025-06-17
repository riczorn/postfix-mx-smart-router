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
    -t, --cache-ttl SEC  Cache TTL in seconds (default: 3600, where 0 disables cache)

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
import time
import dns.resolver
import urllib.parse
import argparse

# Change to the script's directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Default values
DEFAULT_PORT = 10099
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PATTERN_FILE = '/etc/postfix/postfix-mx-pattern-router.conf'
DEFAULT_CACHE_TTL = 3600
DEFAULT_GC_INTERVAL = 3600

# In-memory cache for MX records
mx_cache = {}

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
    parser.add_argument('-t', '--cache-ttl',
                        type=int,
                        default=DEFAULT_CACHE_TTL,
                        help=f'Cache TTL in seconds (default: {DEFAULT_CACHE_TTL}, where 0 disables cache)')
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

def get_mx_records(domain, cache_ttl):
    """Get MX records for a domain using dns.resolver with optional caching."""
    current_time = time.time()

    # Check if caching is enabled (positive TTL) and we have a valid cached entry
    if cache_ttl > 0 and domain in mx_cache:
        cache_time, mx_records = mx_cache[domain]
        if current_time - cache_time < cache_ttl:
            return mx_records

    # No valid cache entry or caching disabled, perform DNS lookup
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        mx_records = [answer.exchange.to_text().rstrip('.').lower() for answer in answers]

        # Cache the result if caching is enabled
        if cache_ttl > 0:
            mx_cache[domain] = (current_time, mx_records)

        return mx_records
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        # Cache empty result if caching is enabled
        if cache_ttl > 0:
            mx_cache[domain] = (current_time, [])

        return []

def cleanup_cache(cache_ttl):
    """Remove expired entries from the cache."""
    if cache_ttl <= 0:
        return 0  # Cache is disabled, nothing to clean up

    current_time = time.time()
    expired_keys = []

    # Identify expired entries
    for domain, (cache_time, _) in mx_cache.items():
        if current_time - cache_time >= cache_ttl:
            expired_keys.append(domain)

    # Remove expired entries
    for domain in expired_keys:
        del mx_cache[domain]

    if expired_keys:
        sys.stdout.write(f"Garbage collection: removed {len(expired_keys)} expired cache entries, new total {len(mx_cache)}\n")
        sys.stdout.flush()

    return len(expired_keys)

def main():
    # Parse command line arguments
    args = parse_arguments()

    # Load patterns from the specified configuration file
    patterns = load_patterns(args.config)
    if not patterns:
        sys.stderr.write(f"Warning: No patterns loaded from {args.config}\n")
        sys.exit(1)

    # Create socket server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((args.host, args.port))
        server.listen(5)
        if args.cache_ttl > 0:
            sys.stdout.write(f"Socketmap server listening on {args.host}:{args.port} (cache {args.cache_ttl} seconds)\n")
        else:
            sys.stdout.write(f"Socketmap server listening on {args.host}:{args.port} (no cache)\n")
        sys.stdout.flush()

        # Initialize last garbage collection time
        last_gc_time = time.time()

        while True:
            # Check if it's time to run garbage collection
            current_time = time.time()
            if args.cache_ttl > 0 and current_time - last_gc_time >= DEFAULT_GC_INTERVAL:
                cleanup_cache(args.cache_ttl)
                last_gc_time = current_time

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
                            mx_records = get_mx_records(domain, args.cache_ttl)

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
                sys.stderr.flush()
            finally:
                conn.close()

    except Exception as e:
        sys.stderr.write(f"Failed to start server: {e}\n")
        sys.stderr.flush()
        sys.exit(1)
    finally:
        server.close()

if __name__ == "__main__":
    main()
