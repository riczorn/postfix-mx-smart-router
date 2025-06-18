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
    --cache-ttl SEC      Cache TTL in seconds (default: 3600, where 0 disables cache)
    --timeout SEC        Client inactivity timeout in seconds (default: 30, where 0 disables timeout)
    -v, --verbose        Increase verbosity level of logging

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
import psutil
import threading

# Change to the script's directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Default values
DEFAULT_PORT = 10099
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PATTERN_FILE = '/etc/postfix/postfix-mx-pattern-router.conf'
DEFAULT_CACHE_TTL = 3600
DEFAULT_CLIENT_TIMEOUT = 60
GC_INTERVAL = 3600
STATS_INTERVAL = 300

# In-memory cache for MX records
mx_cache = {}

# Global args variable
args = None

# Global counter for active connections
active_connections = 0

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
    parser.add_argument('--cache-ttl',
                        type=int,
                        default=DEFAULT_CACHE_TTL,
                        help=f'Cache TTL in seconds (default: {DEFAULT_CACHE_TTL}, where 0 disables cache)')
    parser.add_argument('--timeout',
                        type=int,
                        default=DEFAULT_CLIENT_TIMEOUT,
                        help=f'Client inactivity timeout in seconds (default: {DEFAULT_CLIENT_TIMEOUT}, where 0 disables timeout)')
    parser.add_argument('-v', '--verbose',
                        action='store_true',
                        default=False,
                        help=f'Increase verbosity level (default: false)')
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
    """Get MX records for a domain using dns.resolver with optional caching.

    Returns:
        tuple: (mx_records, from_cache) where:
            - mx_records is a list of MX hostnames
            - from_cache is a boolean indicating if the result came from cache
    """
    current_time = time.time()

    # Check if caching is enabled (positive TTL) and we have a valid cached entry
    if cache_ttl > 0 and domain in mx_cache:
        cache_time, mx_records = mx_cache[domain]
        if current_time - cache_time < cache_ttl:
            return mx_records, True

    # No valid cache entry or caching disabled, perform DNS lookup
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        mx_records = [answer.exchange.to_text().rstrip('.').lower() for answer in answers]

        # Cache the result if caching is enabled
        if cache_ttl > 0:
            mx_cache[domain] = (current_time, mx_records)

        return mx_records, False
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        # Cache empty result if caching is enabled
        if cache_ttl > 0:
            mx_cache[domain] = (current_time, [])

        return [], False

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
        log(f"Garbage collection: removed {len(expired_keys)} expired cache entries, new total {len(mx_cache)}\n", False, True)

    return len(expired_keys)

def print_stats():
    process = psutil.Process(os.getpid())
    memory_usage = process.memory_info().rss / 1024 / 1024  # Convert to MB
    cache_size = len(mx_cache)
    log(f"Memory usage: {memory_usage:.2f} MB, Cache items: {cache_size}, Active connections: {active_connections}\n", False, True)

def bg_thread():
    """Background thread function to periodically report stats and run garbage collection."""
    last_gc_time = time.time()

    while True:
        current_time = time.time()

        # Report stats
        print_stats()

        # Run garbage collection if cache is enabled and it's time
        if args.cache_ttl > 0 and current_time - last_gc_time >= GC_INTERVAL:
            cleanup_cache(args.cache_ttl)
            last_gc_time = current_time

        # Sleep until next interval
        time.sleep(STATS_INTERVAL)


def process_request(request, conn, patterns, cache_ttl):
    if request == 'get *':
        send_response(conn, 500, 'NO RESULT')
        return

    """Process a single request and send the appropriate response."""
    matched = False
    from_cache = False
    domain = None

    # Match 'get email@domain'
    email_match = re.match(r'^get\s+([^@]+@([^@]+))$', request, re.IGNORECASE)
    if email_match:
        email = email_match.group(1).lower()
        parts = email.split('@')
        if len(parts) == 2:
            domain = parts[1]
            mx_records, from_cache = get_mx_records(domain, cache_ttl)

            for mx in mx_records:
                for pattern, relay in patterns.items():
                    if pattern in mx:
                        matched = relay
                        break
                if matched:
                    break

    cache_status = "cache hit" if from_cache else "dns lookup"

    if matched:
        send_response(conn, 200, matched)
        log(f"Match found ({cache_status}): {domain} → {matched}\n")
    else:
        send_response(conn, 500, 'NO RESULT')
        log(f"No match ({cache_status}): {domain}\n", False, True)

def send_response(conn, status_code, message):
    """Send a formatted response to the client with proper encoding."""
    response = f"{status_code} {urllib.parse.quote(message)}\n"
    conn.sendall(response.encode('utf-8'))

def log(message, to_stderr=False, needs_verbose=False):
    """Logs and flushes to stdout/stderr."""
    if (to_stderr):
        sys.stderr.write(message)
    elif (needs_verbose and args.verbose) or not needs_verbose:
        sys.stdout.write(message)
    sys.stdout.flush()

def log_dict(dict, needs_verbose=False):
    for key, value in dict.items():
        log(f"  {key} → {value}\n", False, needs_verbose)

def main():
    # Parse command line arguments
    global args
    args = parse_arguments()

    # Keep track of active connections
    active_connections = 0

    # Load patterns from the specified configuration file
    patterns = load_patterns(args.config)
    if not patterns:
        log(f"Warning: No patterns loaded from {args.config}\n", True)
        sys.exit(1)

    # Create socket server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((args.host, args.port))
        server.listen(5)
        if args.cache_ttl > 0:
            log(f"Socketmap server listening on {args.host}:{args.port} (cache {args.cache_ttl} seconds)\n")
        else:
            log(f"Socketmap server listening on {args.host}:{args.port} (no cache)\n")

        # Print patterns dictionary in a readable format
        log(f"Loaded {(len(patterns))} patterns:\n", False, True)
        log_dict(patterns, True)

        # Start the background thread for stats reporting and garbage collection
        background_thread = threading.Thread(target=bg_thread, daemon=True)
        background_thread.start()

        while True:
            conn, addr = server.accept()
            active_connections += 1

            # Set a timeout for client connections if enabled
            if args.timeout > 0:
                conn.settimeout(args.timeout)

            try:
                while True:
                    data = conn.recv(1024)
                    if not data:  # Connection closed by client
                       log(f"Connection closed by client: {addr}\n")
                       break

                    request = data.decode('utf-8').strip()
                    try:
                        process_request(request, conn, patterns, args.cache_ttl)
                    except Exception as e:
                        log(f"Error processing request: {e}\n", True)
                        send_response(conn, 400, str(e))
                        break

            except Exception as e:
                log(f"Error handling connection: {e}\n", True)
                try:
                    send_response(conn, 400, str(e))
                except:
                    pass

            finally:
                conn.close()
                active_connections -= 1

    except Exception as e:
        log(f"Failed to start server: {e}\n", True)
        sys.exit(1)

    finally:
        server.close()

if __name__ == "__main__":
    main()
