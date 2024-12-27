"""
This Lambda function Moves the source dataset to archive/error folder
"""

# packages
import logging
import json
import boto3 # type: ignore
import os

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


###### lambda primary function ######
def lambda_handler(event, context):  
    """
    This function Moves the source dataset to archive/error folder

    Args:
        event (object): Event data that's passed to the function upon execution
        context (object): Python objects that implements methods and has attributes
    
    Returns:
        NONE
    """
    # Get event details
    s3 = boto3.resource('s3')
    bucket_name = event['taskresult']['bucket_name']
    key_name = event['taskresult']['key_name']
    dest_error_bucket_name = event['taskresult']['error_bucket_name']
    dest_archive_bucket_name = event['taskresult']['archive_bucket_name']
    source_file_name_to_copy = bucket_name + "/" + key_name
    result = {}
    if "error-info" in event:
        status = "FAILURE"
    else:
        status = event['taskresult']['validation']

    # If FAILURE, move to error
    if status == "FAILURE":
        logger_function("Status is set to failure. Moving to error folder", type="info")
        location = dest_error_bucket_name
        s3.Object(dest_error_bucket_name, key_name).copy_from(CopySource=source_file_name_to_copy)
        # TODO update with more descriptive error message
        result['msg'] = f"Batch file moved to {location}/{key_name}"
    # If SUCCESS, move to archive
    elif status == "SUCCESS":
        logger_function("Status is set to archive. Moving to archive folder", type="info")
        location = dest_archive_bucket_name
        s3.Object(dest_archive_bucket_name, key_name).copy_from(CopySource=source_file_name_to_copy)
        # TODO update with more descriptive error message
        result['msg'] = f"Batch file uploaded to RDS and moved to {location}/{key_name} in S3."

    #s3.Object(bucket_name, key_name).delete()
    result['Status'] = status
    return(result)