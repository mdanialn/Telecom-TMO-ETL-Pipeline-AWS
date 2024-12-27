"""
This Lambda function transfers a batch file from the S3 loading zone to raw bucket
"""

# packages
import logging
import json
import boto3  # type: ignore
from botocore.exceptions import ClientError  # type: ignore
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
    
    if 'transaction' in s:
        return 'cof-transactions/'
    elif 'account' in s:
        return 'cof-account-master/'
    elif 'cardholder' in s:
        return 'cof-cardholder-master/'
    elif 'application' in s:
        return 'cof-credit-application/'
    else: 
        logger_function("ERROR: Invalid batch file name.", type="error")
        sys.exit()


###### lambda primary function ######
def lambda_handler(event, context):
    """
    This function moves a batch file from the S3 loading zone to raw bucket

    Args:
        event (object): Event data that's passed to the function upon execution
        context (object): Python objects that implement methods and have attributes
    
    Returns:
        NONE
    """
    try:
        # S3 object
        s3 = boto3.client('s3')

        # Read in SNS message
        sns_message = event['Records'][0]['Sns']['Message']

        # Get batch file name and bucket name from the SNS message
        key_name = json.loads(sns_message)['Records'][0]['s3']['object']['key']
        bucket_name = json.loads(sns_message)['Records'][0]['s3']['bucket']['name']
        
        logger_function(f"Source Bucket Name: {bucket_name}", type="info")
        logger_function(f"Batch File Name: {key_name}", type="info")
        
        # Look in file name to determine the folder path, return folder path for the destination S3 bucket (raw)
        folder_path = contains_keywords(key_name)
        
        # Combine folder path with the original file name
        updated_key_name = f"{folder_path}{key_name.split('/')[-1]}"
        
        logger_function(f"Updated Batch File Name: {updated_key_name}", type="info")

        # Read the file from the source bucket
        response = s3.get_object(Bucket=bucket_name, Key=key_name)
        file_content = response['Body'].read()
        
        # Write the file to the destination bucket
        # TODO: Parameterize hardcoded bucket name
        s3.put_object(Bucket='dev-fnt-0501651-batch-raw', Key=updated_key_name, Body=file_content)
        
        logger_function("S3 Put Object Completed", type="info")
    except ClientError as e:
        logger_function("ERROR: Could not transfer batch file.", type="error")
        logger_function(str(e), type="error")
        sys.exit()
