"""
This Lambda function initiates the AWS Step Functions for ETL orchestration
"""

# packages
import logging
import json
import boto3 # type: ignore
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


###### lambda primary function ######
def lambda_handler(event, context):
    '''
    This function starts AWS Step Functions

    Args:
        event (object): Event data that's passed to the function upon execution
        context (object): Python objects that implement methods and have attributes

    Returns:
        NONE
    '''
    try:
        # Read in SNS message
        sns_message = event['Records'][0]['Sns']['Message']
        
        # Parse the SNS message
        s3_event = json.loads(sns_message)['Records'][0]['s3']

        # Extract bucket details
        source_bucket_name = s3_event['bucket']['name']
        source_bucket_arn = s3_event['bucket']['arn']

        # Extract key details
        source_key_name = s3_event['object']['key']
        
        # Extract the file name from the key
        batch_file_name = source_key_name.split('/')[-1]
        
        schema_bucket_name = 'dev-fnt-0501651-batch-sql-scripts'
        schema_key = 'schema'

        # Create step function input object
        step_function_input = {
            'source_bucket_name': source_bucket_name,
            'source_bucket_arn': source_bucket_arn,
            'source_key_name': source_key_name,
            'batch_file_name': batch_file_name,
            'schema_bucket_name': schema_bucket_name,
            'schema_folder': schema_key
        }
        
        logger_function("Sending JSON dump to Step Functions as input: ", type="info")
        logger_function(step_function_input, type="info")

        client = boto3.client('stepfunctions')
        # TODO: Parameterize hardcoded step_function_arn
        step_function_arn = 'arn:aws:states:us-west-2:905418049473:stateMachine:dev-fnt-0501651-state-machine-batch-etl'
        response = client.start_execution(stateMachineArn=step_function_arn, input=json.dumps(step_function_input))
        
        logger_function("Step Functions successfully initiated with response: {}".format(response), type="info")

    except Exception as e:
        logger_function(f"ERROR: Could not initiate step functions due to {e}.", type="error")
        sys.exit()