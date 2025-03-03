"""
zscaler.py
Methods for working with relevant Zscaler API endpoints
"""
import logging
import configparser
import requests
import sys
import time
import json
import math
from auth.auth import zs_auth
from util.util import increment, log_http_error, listSplit, write_data


config = configparser.ConfigParser()
config.read('config.ini')
zs_config = config['ZSCALER']
zs_hostname = str(zs_config['hostname'])
zs_url_category = "CrowdStrike"
log_config = config['LOG']
data_log = int(log_config['log_indicators'])

def refresh_token():
    """Refreshes Zscaler API Auth token
    returns: Zscaler API Auth token
    """
    return zs_auth()

def validate_category(token):
    """Queries Zscaler API to confirm the configured URL category exists
    token - Zscaler API Auth token
    returns: Entity ID of Zscaler URL Category
    """
    logging.info(f"Confirming URL category {zs_url_category} exists")
    url = f"{zs_hostname}/api/v1/urlCategories?customOnly=true"
    headers = {'content-type': 'application/json',
               'cache-control': 'no-cache',
               'User-Agent' :'Zscaler-FalconX-Intel-Bridge-v2',
               'cookie': "JSESSIONID=" + str(token)}
    response = requests.get(url=url, headers=headers)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as err:
        logging.info(f"[Zscaler API] URL Category validation error: {err}")
        log_http_error(response)
        raise
    url_categories = response.json()
    if len(url_categories) == 0:
        logging.info("No URL categories found")
    else:
        for c in url_categories:
            if c['configuredName'] == zs_url_category:
                logging.info(f"Validated URL category {zs_url_category}")
                return {'id':c['id'], 'content':{'urls':c['urls'][1:], 'dbCategorizedUrls':c['dbCategorizedUrls']}}
    id = create_catagory(token)
    return id

def create_catagory(token):
    """Posts to Zscaler API to create a new URL category with the configured name
    token - Zscaler API Auth token
    returns: entity ID of new Zscaler URL Cateogry
    """
    logging.info(f"Creating URL category {zs_url_category}")
    url = f"{zs_hostname}/api/v1/urlCategories"
    headers = {'content-type': "application/json",
               'cache-control': "no-cache",
               'User-Agent' :'Zscaler-FalconX-Intel-Bridge-v2',
               'cookie': "JSESSIONID=" + str(token)}
    payload = {
            "id": 0,
            "configuredName": zs_url_category,
            "customCategory": "true",
            "superCategory": "USER_DEFINED",
            "urls": ["mine.ppxxmr.com"]
        }
    response = requests.post(url=url, headers=headers, data=json.dumps(payload))
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as err:
        logging.info(f"[Zscaler API] URL Category creation error: {err}")
        log_http_error(response)
        raise
    c = response.json()
    return {'id':c['id'], 'content':{'urls':c['urls'][1:], 'dbCategorizedUrls':c['dbCategorizedUrls']}}

def split_indicators(indicators):
    """Splits a large list of indicators into chunks of 100 for URL lookup
    indicators: list of indicators
    returns: list of lists of indicators
    """
    chunks = [indicators[i:i + 100] for i in range(0, len(indicators), 100)]
    return chunks

def model_chunk(chunk):
    """Transforms Indicator chunks into a Zscaler API ingestable model
    chunk - list of unformatted indicators
    returns: list of formatted indicators
    """
    modeled_urls = []
    categorized = []
    if type(chunk) is not list:
            return {'urls': [], 'dbCategorizedUrls': []}
    for url in chunk:
        try:
            if url['urlClassificationsWithSecurityAlert']:
                pass
            elif 'urlClassifications' not in url:
                modeled_urls.append(url['url'])
            elif 'MISCELLANEOUS_OR_UNKNOWN' in url['urlClassifications']:
                modeled_urls.append(url['url'])
            else:
                categorized.append(url['url'])
        except:
            e = sys.exc_info()[0]
            logging.info(str(e))
            pass
    modeled_chunk = {'urls': modeled_urls,
                     }
    return modeled_chunk

def look_up_indicators(indicators, token):
    """Queries the Zscaler API with indicators to categorize them
    indicators: list of formatted indicators
    token - Zscaler Auth token
    returns: list of indicators ready for ingestion
    """
    logging.info(f"[Zscaler API] Beginning URL look up loop")
    ingestable = {'urls':[]}
    url =  f"{zs_hostname}/api/v1/urlLookup"
    headers = {'content-type': "application/json",
               'cache-control': "no-cache",
               'cookie': "JSESSIONID=" + str(token)}
    chunks = split_indicators(indicators)
    print(f"{'='*20}Zscaler API URL Lookup{'='*20}")
    progress = [0, 0, len(chunks), "Looking up URLs in indicator chunk"]
    for chunk in chunks:
        success  = False
        while not success:
            response = requests.post(url=url, headers=headers, data=json.dumps(chunk))
            if response.status_code == 429:
                r = int(response.headers._store['retry-after'][1])
                logging.info(f"[Zscaler API] Rate limit reached: Sleeping for {r} seconds.")
                time.sleep(r+5)
                continue
            if response.status_code == 409:
                logging.info(f"[Zscaler API] 409 Unknown Error: Sleeping for 10 and retrying 10 seconds.")
                time.sleep(10)
                continue
            if response.status_code == 401:
                logging.info(f"[Zscaler API] 401 Token Expired: Renewing auth and retrying.")
                token = zs_auth()
                headers["cookie"] = "JSESSIONID=" + str(token)
                continue
            if response.status_code == 400:
                logging.info(f"[Zscaler API] 400 Bad Request: One or more indicators in this chunk are incompatible with the URL look-up API. Skipping this chunk.")
                progress = increment(progress, len(chunk))
                time.sleep(1)
                break
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as err:
                logging.info(f"[Zscaler API] URL Lookup Error: {err}")
                log_http_error(response)
                raise
            except requests.exceptions.ConnectionError as err:
                logging.info(f"[Zscaler API] Connection refused error: {err}\nSleeping for 2 minutes.")
                time.sleep(120)
                continue
            success = True
            classified_chunk = response.json()
            modeled_chunk = model_chunk(classified_chunk)
            ingestable['urls'] += modeled_chunk['urls']
            progress = increment(progress, len(chunk))
            time.sleep(1)
    print(f"{'='*29}DONE{'='*29}")
    return ingestable

def push_indicators(token, category, indicators, deleted):
    """Pushes new indicators to the Zscaler API
    token - Zscaler API Auth token
    category - Entity ID of Zscaler URL Category
    indicators - list of indicators to be pushed
    deleted - boolean for new or deleted indicators
    returns: results of push
    """
    action = "ADD_TO_LIST" if not deleted else "REMOVE_FROM_LIST"
    url = f"{zs_hostname}/api/v1/urlCategories/{category}?action={action}"
    headers = {'content-type': "application/json",
               'cache-control': "no-cache",
               'User-Agent' :'Zscaler-FalconX-Intel-Bridge-v2',
               'cookie': "JSESSIONID=" + str(token)}
    progress = [0, 0, len(indicators), "Posting URLs in indicator chunk"]
    print(f"{'='*20 if deleted else '='*22}"
          f"Posting {'Deleted' if deleted else 'New'}* URL's"
          f"{'='*20 if deleted else '='*22}")
    results = put_chunks(indicators, url, headers, progress)
    print(f"{'='*29}DONE{'='*29}")
    if data_log == 1:
        write_data(indicators, deleted)
    return results

def put_chunks(indicators, url, headers, progress):
    """Helper function for push_indicators that makes requests and tracks progress
    indicators - list of indicators
    url - Zscaler API URL
    headers - headers for HTTP request
    progress - progress object
    returns: results of push
    """
    results = []
    indicators = indicators['urls']
    partitions = math.ceil(len(indicators)/5000)
    partitioned_indicators = listSplit(indicators, partitions)
    for chunk in partitioned_indicators:
        success  = False
        # chunk = {'urls': chunk}
        # if not chunk['urls'] and not chunk['dbCategorizedUrls']:
        #         progress[0] = progress[0] + 1
        #         progress[1] = progress[1] + 1
        #         continue
        while not success:
            payload = {"customCategory": "true",
                    "superCategory": "USER_DEFINED",
                    "urls": chunk,
                    #"dbCategorizedUrls": chunk['dbCategorizedUrls'],
                    "configuredName": zs_url_category
            }
            response = requests.put(url=url,headers=headers, data=json.dumps(payload))
            if response.status_code == 429:
                r = int(response.headers._store['retry-after'][1])
                logging.info(f"[Zscaler API] Rate limit reached: Sleeping for {r} seconds.")
                time.sleep(r+5)
                continue 
            if response.status_code == 409:
                logging.info(f"[Zscaler API] 409 Unknown Error: Sleeping for 10 and retrying 10 seconds.")
                time.sleep(10)
                continue
            if response.status_code == 401:
                logging.info(f"[Zscaler API] 401 Token Expired: Renewing auth and retrying.")
                token = zs_auth()
                headers["cookie"] = "JSESSIONID=" + str(token)
                continue
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as err:
                logging.info(f"[Zscaler API] Add new URLs error: {err}")
                log_http_error(response)
                raise
            except requests.exceptions.ConnectionError as err:
                logging.info(f"[Zscaler API] Connection refused error: {err}\nSleeping for 2 minutes.")
                time.sleep(120)
                continue
            success = True
            progress = increment(progress, len(chunk))
            result = response.json()
            time.sleep(1)
    results.append(result)
    return results

def save_changes(token):
    """Posts to Zscaler API to activate changes made in current etl_loop
    token - Zscaler API Auth token
    returns: HTTP results
    """
    logging.info(f"[Zscaler API] Activating changes")
    status_url = f"{zs_hostname}/api/v1/status"
    save_url = f"{zs_hostname}/api/v1/status/activate"
    headers = {'content-type': "application/json",
               'cache-control': "no-cache",
               'User-Agent' :'Zscaler-FalconX-Intel-Bridge-v2',
               'cookie': "JSESSIONID=" + str(token)}
    status_response = requests.get(url=status_url, headers=headers)
    try:
        status_response.raise_for_status()
    except requests.exceptions.HTTPError as err:
        logging.info(f"[Zscaler API] Get change status error: {err}")
        log_http_error(status_response)
        raise
    status = status_response.json()
    logging.info(f"[Zscaler API] New change status: {json.dumps(status)}")
    activate_response = requests.post(url=save_url, headers=headers)
    try:
        activate_response.raise_for_status()
    except requests.exceptions.HTTPError as err:
        logging.info(f"[Zscaler API] Activate Changes error: {err}")
        log_http_error(status_response)
        raise
    activate = activate_response.json()
    logging.info(f"[Zscaler API] Changes activated: {json.dumps(activate)}")
    return activate
