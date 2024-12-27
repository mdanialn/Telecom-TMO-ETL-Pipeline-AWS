"""
This Lambda function starts the AWS glue job
"""

# packages
import logging
import json
import boto3
import os
import sys

# initiate logger for CloudWatch
logger = logging.getLogger()
logger.setLevel(logging.INFO)


###### logger function ######
def logger_function(message, type="info"):
    """
    Helper function for providing logger messages for CloudWatch

    Args:
        message (string): The message to display
        type (string): Either "info" or "error"
    """
    if type == 'info':
        logger.info(message)
    elif type == 'error':
        logger.error(message)

    return


###### keyword function ######
def contains_keywords(s):
    # Remove spaces and convert to lowercase
    s = s.replace(" ", "").lower()
    
    keywords = ["transaction", "account", "cardholder", "application"]
    
    # TODO update with correct glue job name
    if 'transaction' in s:
        return 'dev-fnt-0501651-glue-batch-transactions'
    elif 'account' in s:
        return 'dev-fnt-0501651-glue-batch-account-summary'
    elif 'cardholder' in s:
        return 'dev-fnt-0501651-glue-batch-cardholder'
    elif 'application' in s:
        return 'dev-fnt-0501651-glue-batch-application'
    else: 
        logger_function("ERROR: Invalid batch file name.", type="error")
        sys.exit()


###### lambda primary function ######
def lambda_handler(event, context):
    """
    This function sends the file name to AWS Glue and starts the ETL job

    Args:
        event (object): Event data that's passed to the function upon execution
        context (object): Python objects that implements methods and has attributes
    
    Returns:
        NONE
    """
    # Event is result object from previous step
    job_name = event['taskresult']['job_name']
    key_name = event['taskresult']['key_name']
    bucket_name = event['taskresult']['bucket_name']
    batch_file_name = event['taskresult']['latestBatchFileName']
    batch_timestamp = event['taskresult']['latestBatchTimestamp']
    # TODO update with correct destination bucket name
    dest_bucket_name = 'dev-fnt-0501651-batch-stage'

    # Use batch file name to determine Glue Job
    job_name = contains_keywords(key_name)
    
    # Send object to glue client & start
    client = boto3.client('glue')
    client.start_job_run(
        JobName = job_name,
        Arguments = {
            '--source_key':key_name,
            '--source_bucket': bucket_name,
            '--dest_bucket': dest_bucket_name,
            '--batch_file_name': batch_file_name,
            '--batch_timestamp': batch_timestamp
        }
    )
    
    return {
        'statusCode': 200,
        'body': json.dumps('Glue Job initiated from Lambda')
    }