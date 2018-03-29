#!/usr/bin/python
# -*- coding: utf-8 -*-

import logging
import requests
import sys
import time

from threading import Thread
from random import randint
from utils import get_async_requests_session

log = logging.getLogger(__name__)

# Last used proxy for round-robin.
last_proxy = -1
ptc_last_proxy = -1

# Proxy check result constants.
check_result_ok = 0
check_result_failed = 1
check_result_banned = 2
check_result_wrong = 3
check_result_timeout = 4
check_result_exception = 5
check_result_empty = 6
check_result_max = 6  # Should be equal to maximal return code.


# Evaluates the status of PTC and Niantic request futures, and returns the
# result (optionally with an error).
# Warning: blocking! Can only get status code if request has finished.
def get_proxy_test_status(proxy, future_ptc, future_niantic):
    # Start by assuming everything is OK.
    check_result = check_result_ok
    proxy_error = None

    # Make sure we don't trip any code quality tools that test scope.
    ptc_response = None
    niantic_response = None

    # Make sure both requests are completed.
    try:
        if future_ptc and future_niantic:
            ptc_response = future_ptc.result()
            niantic_response = future_niantic.result()
        elif future_ptc:
            ptc_response = future_ptc.result()
        else:
            niantic_response = future_niantic.result()
    except requests.exceptions.ConnectTimeout:
        proxy_error = ('Connection timeout for'
                       + ' proxy {}.').format(proxy)
        check_result = check_result_timeout
    except requests.exceptions.ConnectionError:
        proxy_error = 'Failed to connect to proxy {}.'.format(proxy)
        check_result = check_result_failed
    except Exception as e:
        proxy_error = e
        check_result = check_result_exception

    # If we've already encountered a problem, stop here.
    if proxy_error:
        return (proxy_error, check_result)

    # Evaluate response status code.
    if ptc_response:
        ptc_status = ptc_response.status_code
    if niantic_response:
        niantic_status = niantic_response.status_code

    banned_status_codes = [403, 409]

    if ptc_status and niantic_status:
        if niantic_status == 200 and ptc_status == 200:
            log.debug('Proxy %s is ok.', proxy)
        elif (niantic_status in banned_status_codes or
              ptc_status in banned_status_codes):
            proxy_error = ('Proxy {} is banned -'
                           + ' got PTC status code: {}, Niantic status'
                           + ' code: {}.').format(proxy,
                                                  ptc_status,
                                                  niantic_status)
            check_result = check_result_banned
        else:
            proxy_error = ('Wrong status codes -'
                           + ' PTC: {},'
                           + ' Niantic: {}.').format(ptc_status,
                                                     niantic_status)
            check_result = check_result_wrong
    elif ptc_status:
        if ptc_status == 200:
            log.debug('Proxy %s is ok for ptc.', proxy)
        elif (ptc_status in banned_status_codes):
            proxy_error = ('Proxy {} is banned -'
                           + ' got PTC status code: {}').format(proxy,
                                                                ptc_status)
            check_result = check_result_banned
        else:
            proxy_error = ('Wrong status codes -'
                           + ' PTC: {}').format(ptc_status)
            check_result = check_result_wrong
    else:
        if niantic_status == 200:
            log.debug('Proxy %s is ok for pogo.', proxy)
        elif (niantic_status in banned_status_codes):
            proxy_error = ('Proxy {} is banned -'
                           + ' got pogo status code: {}').format(proxy,
                                                                 niantic_status)
            check_result = check_result_banned
        else:
            proxy_error = ('Wrong status codes -'
                           + ' pogo: {}').format(ptc_status)
            check_result = check_result_wrong

    # Explicitly release connection back to the pool, because we don't need
    # or want to consume the content.
    if ptc_response:
        ptc_response.close()
    if niantic_response:
        niantic_response.close()

    return (proxy_error, check_result)


# Requests to send for testing, which returns futures for Niantic and PTC.
def start_request_futures(ptc_session, niantic_session, proxy, timeout):
    # URLs for proxy testing.
    proxy_test_url = 'https://pgorelease.nianticlabs.com/plfe/version'
    proxy_test_ptc_url = 'https://sso.pokemon.com/sso/login' \
                         '?service=https%3A%2F%2Fsso.pokemon.com' \
                         '%2Fsso%2Foauth2.0%2FcallbackAuthorize' \
                         '&locale=en_US'

    log.debug('Checking proxy: %s.', proxy)

    future_ptc = None
    future_niantic = None
    # Send request to pokemon.com if niantic
    if ptc_session:
        future_ptc = ptc_session.get(
            proxy_test_ptc_url,
            proxies={'http': proxy, 'https': proxy},
            timeout=timeout,
            headers={'Host': 'sso.pokemon.com',
                     'Connection': 'close',
                     'Accept': '*/*',
                     'User-Agent': 'pokemongo/0 CFNetwork/893.14.2 Darwin/17.3.0',
                     'Accept-Language': 'en-us',
                     'Accept-Encoding': 'br, gzip, deflate',
                     'X-Unity-Version': '2017.1.2f1'},
            background_callback=__proxy_check_completed,
            stream=True)
    # Send request to nianticlabs.com if pogo
    if niantic_session:
        future_niantic = niantic_session.get(
            proxy_test_url,
            proxies={'http': proxy, 'https': proxy},
            timeout=timeout,
            headers={'Host': 'pgorelease.nianticlabs.com',
                     'Connection': 'close',
                     'Accept': '*/*',
                     'User-Agent': 'pokemongo/0 CFNetwork/893.14.2 Darwin/17.3.0',
                     'Accept-Language': 'en-us',
                     'Accept-Encoding': 'br, gzip, deflate',
                     'X-Unity-Version': '2017.1.2f1'},
            background_callback=__proxy_check_completed,
            stream=True)

    # Return futures.
    return (future_ptc, future_niantic)


# Load proxies and return a list.
def load_proxies(args, ptc = None):
    proxies = []

    # If it's pogo proxy.
    if not ptc:
        # Load proxies from the file. Override args.proxy if specified.
        if args.proxy_file is not None:
            log.info('Loading pogo proxies from file.')
        
            with open(args.proxy_file) as f:
                for line in f:
                    stripped = line.strip()
        
                    # Ignore blank lines and comment lines.
                    if len(stripped) == 0 or line.startswith('#'):
                        continue
        
                    proxies.append(stripped)
        
            log.info('Loaded %d pogo proxies.', len(proxies))
        
            if len(proxies) == 0:
                log.error('Pogo proxy file was configured but ' +
                          'no proxies were loaded. Aborting.')
                sys.exit(1)
        elif args.proxy:
            if isinstance(args.proxy, list):
                proxies = args.proxy
            else:
                proxies.append(args.proxy)
        
        # No proxies - no cookies.
        if (proxies is None) or (len(proxies) == 0):
            log.info('No proxies are configured.')
            return None
    # Else it's ptc proxy
    else:
        # Load proxies from the file. Override args.proxy if specified.
        if args.ptc_proxy_file is not None:
            log.info('Loading ptc proxies from file.')
        
            with open(args.ptc_proxy_file) as f:
                for line in f:
                    stripped = line.strip()
        
                    # Ignore blank lines and comment lines.
                    if len(stripped) == 0 or line.startswith('#'):
                        continue
        
                    proxies.append(stripped)
        
            log.info('Loaded %d ptc proxies.', len(proxies))
        
            if len(proxies) == 0:
                log.error('ptc poxy file was configured but ' +
                          'no proxies were loaded. Aborting.')
                sys.exit(1)
        elif args.ptc_proxy:
            if isinstance(args.ptc_proxy, list):
                proxies = args.ptc_proxy
            else:
                proxies.append(args.ptc_proxy)
        
        # No proxies - no cookies.
        if (proxies is None) or (len(proxies) == 0):
            log.info('No ptc proxies are configured.')
            return None
    return proxies


# Check all proxies and return a working list with proxies.
def check_proxies(args, proxies, ptc = None):
    total_proxies = len(proxies)

    # Store counter per result type.
    check_results = [0] * (check_result_max + 1)

    # If proxy testing concurrency is set to automatic, use max.
    proxy_concurrency = args.proxy_test_concurrency

    if args.proxy_test_concurrency == 0:
        proxy_concurrency = total_proxies

    if proxy_concurrency >= 100:
        log.warning(
            "Starting proxy test for %d proxies with %d concurrency. If this" +
            " concurrency level breaks the map for you, consider lowering it.",
            total_proxies, proxy_concurrency)

    # Get persistent session per host.
    # TODO: Rework API request wrapper so requests are retried, then increase
    # the # of retries to allow for proxies.
    ptc_session = None
    niantic_session = None
    if ptc:
        ptc_session = get_async_requests_session(
            args.proxy_test_retries,
            args.proxy_test_backoff_factor,
            proxy_concurrency)
    else:
        niantic_session = get_async_requests_session(
            args.proxy_test_retries,
            args.proxy_test_backoff_factor,
            proxy_concurrency)

    # List to hold background workers.
    proxy_queue = []
    working_proxies = []
    show_warnings = total_proxies <= 10

    log.info('Checking %d proxies...', total_proxies)
    if not show_warnings:
        log.info('Enable -v or -vv to see proxy testing details.')

    # Start async requests & store futures.
    for proxy in proxies:
        future_ptc, future_niantic = start_request_futures(
            ptc_session,
            niantic_session,
            proxy,
            args.proxy_test_timeout)

        proxy_queue.append((proxy, future_ptc, future_niantic))

    # Wait here until all items in proxy_queue are processed, so we have a list
    # of working proxies. We intentionally start all requests before handling
    # them so they can asynchronously continue in the background, even as we're
    # blocking to wait for one. The double loop is intentional.
    for proxy, future_ptc, future_niantic in proxy_queue:
        error, result = get_proxy_test_status(proxy,
                                              future_ptc,
                                              future_niantic)

        check_results[result] += 1

        if error:
            # Decrease output amount if there are a lot of proxies.
            if show_warnings:
                log.warning(error)
            else:
                log.debug(error)
        else:
            working_proxies.append(proxy)

    num_working_proxies = len(working_proxies)

    if num_working_proxies == 0:
        log.error('Proxy was configured but no working'
                  + ' proxies were found. Exiting process.')
        sys.exit(1)
    else:
        other_fails = (check_results[check_result_failed] +
                       check_results[check_result_wrong] +
                       check_results[check_result_exception] +
                       check_results[check_result_empty])
        log.info('Proxy check completed. Working: %d, banned: %d,'
                 + ' timeout: %d, other fails: %d of total %d configured.',
                 num_working_proxies, check_results[check_result_banned],
                 check_results[check_result_timeout],
                 other_fails,
                 total_proxies)

        return working_proxies


# Thread function for periodical proxy updating.
def proxies_refresher(args):
    while True:
        # Wait before refresh, because initial refresh is done at startup.
        time.sleep(args.proxy_refresh)
        
        if args.proxy_file:
            try:
                proxies = load_proxies(args)
            
                if not args.proxy_skip_check:
                    proxies = check_proxies(args, proxies)
            
                # If we've arrived here, we're guaranteed to have at least one
                # working proxy. check_proxies stops the process if no proxies
                # are left.
            
                args.proxy = proxies
                log.info('Regular pogo proxy refresh complete.')
            except Exception as e:
                log.exception('Exception while refreshing pogo proxies: %s.', e)
        if args.ptc_proxy_file:
            try:
                proxies = load_proxies(args, ptc = True)
            
                if not args.proxy_skip_check:
                    proxies = check_proxies(args, proxies)
            
                # If we've arrived here, we're guaranteed to have at least one
                # working proxy. check_proxies stops the process if no proxies
                # are left.
            
                args.proxy = proxies
                log.info('Regular pogo proxy refresh complete.')
            except Exception as e:
                log.exception('Exception while refreshing pogo proxies: %s.', e)


# Provide new proxy for a search thread.
def get_new_proxy(args):
    global last_proxy

    # If round - simply get next proxy.
    if (args.proxy_rotation == 'round'):
        if last_proxy >= len(args.proxy) - 1:
            last_proxy = 0
        else:
            last_proxy = last_proxy + 1
        lp = last_proxy
    # If random - get random one.
    elif (args.proxy_rotation == 'random'):
        lp = randint(0, len(args.proxy) - 1)
    else:
        log.warning('Parameter -pxo/--proxy-rotation has wrong value. ' +
                    'Use only first proxy.')
        lp = 0

    return lp, args.proxy[lp]

# Provide new proxy for a search thread.
def get_ptc_new_proxy(args):
    global ptc_last_proxy

    # If round - simply get next proxy.
    if (args.proxy_rotation == 'round'):
        if ptc_last_proxy >= len(args.ptc_proxy) - 1:
            ptc_last_proxy = 0
        else:
            ptc_last_proxy = ptc_last_proxy + 1
        lp = ptc_last_proxy
    # If random - get random one.
    elif (args.proxy_rotation == 'random'):
        lp = randint(0, len(args.ptc_proxy) - 1)
    else:
        log.warning('Parameter -pxo/--proxy-rotation has wrong value. ' +
                    'Use only first proxy.')
        lp = 0

    return lp, args.ptc_proxy[lp]


def initialize_proxies(args):
    # Processing proxies if set (load from file, check and overwrite old
    # args.proxy with new working list).
    args.proxy = load_proxies(args)
    args.ptc_proxy = load_proxies(args, ptc = True)

    if args.proxy and not args.proxy_skip_check:
        args.proxy = check_proxies(args, args.proxy)
    if args.ptc_proxy and not args.proxy_skip_check:
        args.ptc_proxy = check_proxies(args, args.ptc_proxy)

    # Run periodical proxy refresh thread.
    if (args.proxy_file is not None) and (args.proxy_refresh > 0):
        t = Thread(target=proxies_refresher,
                   name='proxy-refresh', args=(args,))
        t.daemon = True
        t.start()
    else:
        log.info('Periodical proxies refresh disabled.')


# Background handler for completed proxy check requests.
# Currently doesn't do anything.
def __proxy_check_completed(sess, resp):
    pass
