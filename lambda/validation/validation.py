"""
This Lambda function validates batch file schema and records
"""

# packages
import json
import pandas as pd
from datetime import datetime
import boto3  # type: ignore
import os
from io import BytesIO
import logging
import io
import csv
import re

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

    keywords = {
        'transaction': 'dev-fnt-0501651-glue-batch-transactions',
        'account': 'dev-fnt-0501651-glue-batch-account-summary',
        'cardholder': 'dev-fnt-0501651-glue-batch-cardholder',
        'application': 'dev-fnt-0501651-glue-batch-application'
    }

    if 'transaction' in s:
        return keywords['transaction']
    elif 'account' in s:
        return keywords['account']
    elif 'cardholder' in s:
        return keywords['cardholder']
    elif 'application' in s:
        return keywords['application']
    else:
        logger_function("ERROR: Invalid batch file name.", type="error")
        return 'error'


###### schema function ######
def get_schema(file_name, bucket_name, schema_folder):
    """
    Helper function to fetch and return the schema from S3 based on the batch file name.
    Args:
        file_name (string): The batch file name to match schema (e.g., 'account', 'transaction')
        bucket_name (string): The S3 bucket where schemas are stored
        schema_folder (string): The folder path in S3 where schemas are located
    Returns:
        schema (list): The schema loaded from the corresponding CSV file
    """

    s3 = boto3.client('s3')

    # Define the schema file path based on the file type in the batch file name
    if 'DailyTransactions' in file_name:
        schema_key = f"{schema_folder}/transaction_schema.csv"
    elif 'DailyAccountMaster' in file_name:
        schema_key = f"{schema_folder}/account_schema.csv"
    elif 'cardholder' in file_name:
        schema_key = f"{schema_folder}/cardholder_schema.csv"
    elif 'application' in file_name:
        schema_key = f"{schema_folder}/application_schema.csv"

    schema = []

    # Open the schema CSV file and read its contents into a list
    try:
        # Fetch the schema file from S3
        response = s3.get_object(Bucket=bucket_name, Key=schema_key)
        schema_content = response['Body'].read().decode('utf-8-sig')

        # Read the CSV content using csv.DictReader
        csv_reader = csv.DictReader(io.StringIO(schema_content))

        # Convert CSV rows into a dictionary, assuming schema has field names and types
        for row in csv_reader:
            field_name = row['field_name']
            schema_row = {}
            schema_row[field_name] = {
                'nullable': row['nullable'],
                'type': row['datatype'],
                'date_conversion': row['date_conversion'],
                'default': row['default'],
                'length': int(row['length']) if row['length'].isdigit() else None
            }

            schema.append(schema_row)

        return schema

    except Exception as e:
        logger_function(f"Error reading schema file from S3: {e}", type="error")
        return []


# Function to validate records based on the schema and batch file
def validate_records(df_source, schema):
    """
    Validate only 10 records in the batch file based on the schema.
    
    Args:
        df_source (DataFrame): The batch file loaded as a DataFrame.
        schema (list): The schema loaded from the schema CSV file.
    
    Returns:
        all_errors (list): A list of validation error messages across all records.
    """
    # Prepare schema dictionary for validation
    schema_dict = {}
    for index, field_schema in enumerate(schema):
        for field_name, field_properties in field_schema.items():
            schema_dict[index] = field_properties  

    # Validate records and collect errors
    all_errors = []
    for record_index, record in df_source.iterrows():
        if record_index > 9:  # Validate first 10 records
            break
        errors = validate_record(record, record_index, schema_dict)
        all_errors.extend(errors)  # Collect all errors from each record

    return all_errors
    
def validate_record(record, record_index, schema_dict):
    """
    Validate a single record based on the schema.
    
    Args:
        record (Series): The record values as a pandas Series.
        record_index (int): The index of the record being processed.
        schema_dict (dict): The schema dictionary that contains field types and validation rules.

    Returns:
        errors (list): A list of validation error messages for this record.
    """
    errors = []

    # Lambda functions for date and datetime parsing
    to_datetime = lambda d: datetime.strptime(d, "%Y-%m-%dT%H:%M:%S.%fZ")
    to_date = lambda d: datetime.strptime(d, '%Y-%m-%d')

    for index, value in enumerate(record):
        if index in schema_dict:
            field_schema = schema_dict[index]
            expected_type = field_schema['type']
            nullable = field_schema['nullable']
            max_length = field_schema.get('length')  
            
            # Check for empty or null values
            if pd.isna(value) or str(value).strip() == '':
                if nullable.lower() == 'true':
                    continue  
                else:
                    # If nullable is false, add error
                    errors.append(f"Record {record_index + 1}: Field {index + 1} cannot be null or empty.")
                    continue

            # Perform validation only if the value is non-null
            if value:
                if expected_type in ['Small Char', 'Medium Char', 'Long Char'] and not isinstance(value, str):
                    errors.append(f"Record {record_index + 1}: Field {index + 1} should be a Text (String).")
                    
                # elif expected_type in ['Small Num', 'Medium Num']:
                #     try:
                #         # Attempt to convert the value to an integer
                #         int_value = int(value)
                #     except ValueError:
                #         # If conversion fails, it's not a valid integer, so we append an error
                #         errors.append(f"Record {record_index + 1}: Field {index + 1} should be an Integer (Small Num, Medium Num).")

                
                elif expected_type in ['Decimal']:
                    try:
                        # Attempt to convert the value to a decimal (float)
                        decimal_value = float(value)
                    except ValueError:
                        # If conversion fails, it's not a valid decimal, so we append an error
                        errors.append(f"Record {record_index + 1}: Field {index + 1} should be a Decimal value.")
                
                elif expected_type == 'Date':
                    # Convert date if in the form YYYYMMDD to YYYY-MM-DD
                    if len(value) == 8 and value.isdigit():
                        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
                
                    try:
                        to_date(value)  
                    except ValueError:
                        errors.append(f"Record {record_index + 1}: Field {index + 1} should be a valid Date (YYYY-MM-DD).")

                elif expected_type == 'timestamptz':
                    try:
                        to_datetime(value)  
                    except ValueError:
                        errors.append(f"Record {record_index + 1}: Field {index + 1} should be a valid Timestamp with Timezone (timestamptz).")
                
                ##TODO resolve issue of 2 extra characters for Date fields 

                #  Length validation with reduced length if Date or timestamptz type
                field_length = len(value)  
                if expected_type in ['Date', 'timestamptz']:
                    field_length -= 2  

                if max_length and field_length > max_length:
                    errors.append(f"Record {record_index + 1}: Field {index + 1} exceeds max length {max_length} (Actual length: {field_length}).")
    return errors


###### lambda primary function ######
def lambda_handler(event, context):
    """
    This function validates batch file schema and records and sends results to the step function.
    Args:
        event (object): Event data passed to the function upon execution
        context (object): Lambda context object
    Returns:
        result (dict): Result dictionary containing validation status and other metadata
    """
    
    # Initialize the result dictionary to process the result in next stage 
    result = {
        'validation': "UNKNOWN",  # Default value to indicate unknown status
        'reason': "",
        'location': ""
    }
    
    try:
        # S3 object
        s3 = boto3.client('s3')

        # Event is result object from previous step
        key_name = event.get('source_key_name', "")
        bucket_name = event.get('source_bucket_name', "")
        bucket_arn = event.get('source_bucket_arn', "")
        file_name = event.get('batch_file_name', "")
        schema_bucket = event.get('schema_bucket_name', "")
        schema_folder = event.get('schema_folder', "")

        logger_function(f"Source Bucket Name: {bucket_name}", type="info")
        logger_function(f"Batch File Name: {file_name}", type="info")
        logger_function(f"schema bucket name: {schema_bucket}", type='info')

        # Look in file name to key on what batch file it is, return aws glue job name
        job_name = contains_keywords(file_name)

        # Create result object to move to next step
        result.update({
            'bucket_name': bucket_name,
            'bucket_arn': bucket_arn,
            'key_name': key_name,
            'job_name': job_name,
            'error_bucket_name': "dev-fnt-0501651-batch-error",
            'archive_bucket_name': "dev-fnt-0501651-batch-archive",
            'stage_bucket_name': "dev-fnt-0501651-batch-stage",
            'validation': "SUCCESS"
        })
    except Exception as e:
        result['validation'] = "FAILURE"
        result['reason'] = f"Could not initiate validation: {e}"
        result['location'] = 'error'
        logger_function(f"ERROR: Could not initiate validation due to {e}.", type="error")
        return result

    if job_name == 'error':
        result['validation'] = "FAILURE"
        result['reason'] = "Invalid batch file name"
        result['location'] = 'error'
        logger_function("ERROR: Invalid batch file name.", type="error")
        return result
        
    # Get schema and log the outcome
    try:
        schema = get_schema(file_name=file_name, bucket_name=schema_bucket, schema_folder=schema_folder)
        if schema:
            result['validation'] = "SUCCESS"
            logger_function(f"Successfully fetched schema for {file_name}", type="info")
        else:
            raise ValueError(f"Schema for {file_name} is empty or could not be fetched.")
    except Exception as e:
        result['validation'] = "FAILURE"
        result['reason'] = f"Error fetching schema: {e}"
        result['location'] = 'error'
        logger_function(f"Error fetching schema for {file_name}: {e}", type="error")
        return result

    # Fetch the batch file from the S3 bucket
    try:
        response = s3.get_object(Bucket=bucket_name, Key=key_name)
        file_content = response['Body'].read()

        # Reading the batch file content as a DataFrame, and check it has records 
        
        df_source = pd.read_csv(io.BytesIO(file_content), delimiter='|',  header=None, skiprows=1, skipfooter=1, engine='python', on_bad_lines='skip',  dtype=str)
        logger_function(f"{file_name} has records", type="info")
        result['validation'] = "SUCCESS" 
    except pd.errors.EmptyDataError:
        result['validation'] = "FAILURE"
        result['reason'] = f"{file_name} has no records"
        result['location'] = 'error'
        logger_function(f"ERROR: {file_name} has no records", type="error")
        return result 
    except Exception as e:
        result['validation'] = "FAILURE"
        result['reason'] = f"An unexpected error occurred: {e}"
        result['location'] = 'error'
        logger_function(f"ERROR: An unexpected error occurred while reading {file_name}: {e}", type="error")
        return result  

    #Load the trailer row separately and validate row count 
    try:
        trailer_row = pd.read_csv(
            io.BytesIO(file_content),
            delimiter='|',
            header=None,
            skiprows=len(df_source) + 1,  # Skip data rows and header row
            nrows=1,
            dtype=str
        )
        expected_row_count = int(trailer_row.iloc[0, 1])
        actual_row_count = len(df_source)
        if actual_row_count != expected_row_count:
            raise ValueError(f"Row count mismatch: Expected {expected_row_count}, but found {actual_row_count}")

        # Log success if row count matches
        logger_function(f"Row count validation passed: {actual_row_count} rows", type="info")
        result['validation'] = "SUCCESS"
    except ValueError as ve:
        result['validation'] = "FAILURE"
        result['reason'] = f"Row count validation failed: {ve}"
        result['location'] = 'error'
        logger_function(f"Row count validation failed: {ve}", type="error")
        return result 
    except Exception as e:
        result['validation'] = "FAILURE"
        result['reason'] = f"Unexpected error in row count validation: {e}"
        result['location'] = 'error'
        logger_function(f"Unexpected error in row count validation: {e}", type="error")
        return result 
    
    # Use regex to extract both the date (YYYYMMDD) and time (HHMMSS)
    match = re.search(r'(\d{8})\.(\d{6})', file_name)
    if match:
        # Extract the matched strings for date and time
        file_date_str = match.group(1)  # First match: YYYYMMDD
        file_time_str = match.group(2)  # Second match: HHMMSS
        combined_datetime_str = file_date_str + file_time_str

        try:
            # Validate the extracted string as a valid date and time (YYYYMMDDHHMMSS)
            combined_datetime_str = str(combined_datetime_str)
            file_datetime = datetime.strptime(combined_datetime_str, '%Y%m%d%H%M%S')
            logger_function(f"batch file has Valid date and time: {file_datetime}", type="info")
            result['validation'] = "SUCCESS"
        except ValueError as e:
            result['validation'] = "FAILURE"
            result['reason'] = "Invalid date and time format in file name"
            result['location'] = 'error'
            logger_function(f"ERROR: Invalid date and time format in file name: {e}", type="error")
            return result  

    # Validate the records against the schema
    try:
        all_errors = validate_records(df_source, schema)

        # Check if there are any validation errors
        if all_errors:
            result['validation'] = "FAILURE"
            result['errors'] = all_errors
            result['reason'] = "Record validation failed."
            result['location'] = 'error'
            logger_function(f"ERROR: Validation failed for {file_name} with errors: {all_errors}", type="error")
            return result
        else:
            logger_function(f"Record validation successful for {file_name}.", type="info")
            result['validation'] = "SUCCESS"
    except Exception as e:
        result['validation'] = "FAILURE"
        result['reason'] = f"An unexpected error occurred during validation: {e}"
        result['location'] = 'error'
        logger_function(f"ERROR: An unexpected error occurred during record validation: {e}", type="error")
        return result

    if result['validation'] == "SUCCESS":
        logger_function(f"Batch file {key_name} is valid", type="info")
        logger_function("Moving batch file to next step.", type="info")
        result['latestBatchFileName'] = file_name
        result['latestBatchTimestamp'] = combined_datetime_str
        return result
