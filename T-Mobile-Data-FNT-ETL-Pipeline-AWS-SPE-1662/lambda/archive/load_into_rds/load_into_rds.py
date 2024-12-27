"""
This Lambda function connects to Aurora Postgres and loads batch file parquets into RDS
"""

# packages
import pandas as pd
import sys
import logging
import psycopg2
import json
import boto3
from botocore.exceptions import ClientError
import io

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


###### secret manager function ######
def get_db_secret(secret_name, region_name):
    """
    Helper function for retrieving connection credentials for Aurora Postgres stored in SecretManager

    Args:
        secret_name (string): Name of stored secret in SecretManager
        region_name (string): AWS region name where secret is stored

    Returns:
        credential (dict): Dictionary containing secret key:value pairs for database connection
    """
    credential = {}

    # create boto3 session to connect to client
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )

    # store secret response
    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        raise e
    secret = json.loads(get_secret_value_response['SecretString'])

    # assign secret key:value pairs to "credential" and return
    credential['USER_NAME'] = secret['username']
    credential['PASSWORD'] = secret['password']
    credential['RDS_HOST'] = secret['host']
    credential['RDS_PORT'] = secret['port']
    credential['DB_NAME'] = secret['dbClusterIdentifier']
    credential['ENGINE'] = secret['engine']

    return credential


###### database connector function ######
def db_connector(credential):
    """
    This function creates the connection object for Aurora Postgres

    Args:
        credential (dict): Dictionary containing secret key:value pairs for database connection

    Returns
        conn (object): Connection object on Aurora Postgres instance
    """

    # format only needed key:values from credential for connection string
    user_name = credential['USER_NAME']
    password = credential['PASSWORD']
    rds_host = credential['RDS_HOST']
    rds_port = credential['RDS_PORT']

    # create connection
    try:
        conn = psycopg2.connect(host=rds_host,
                                user=user_name,
                                password=password,
                                port=rds_port)
        conn.autocommit = True
        logger_function("SUCCESS: Connection to Aurora Postgres instance succeeded.", type="info")
    except psycopg2.Error as e:
        logger_function("ERROR: Could not connect to Postgres instance.", type="error")
        logger_function(e, type="error")
        sys.exit()

    # return connection object
    return conn


###### keyword function ######
def contains_keywords(s):
    # Remove spaces and convert to lowercase
    s = s.replace(" ", "").lower()
    
    keywords = ["transaction", "account", "cardholder", "application"]
    
    if 'transaction' in s:
        return 'cof-transactions/transaction_batch_file.dat'
    elif 'account' in s:
        return 'cof-account-master/account_batch_file.dat'
    elif 'cardholder' in s:
        return 'cof-cardholder-master/cardholder_batch_file.dat'
    elif 'application' in s:
        return 'cof-credit-application/credit_application_batch_file.dat'
    else: 
        logger_function("ERROR: Invalid batch file name.", type="error")
        sys.exit()


###### temp table function ######
def get_temp_table_schema(batch_file):
    """
    This function uses the file name uploaded to S3 to identify the temporary table that should be created

    Args:
        s3_file_name (string): S3 file (path and file) identifying which object is causing Lambda trigger

    Returns
        sql1 (string): SQL statement used to create the temp table in Aurora Postgres
        sql2 (string): SQL statement used to delete records from temp table
        temp_tbl_name (string): Name of temporary table
    """

    if batch_file == 'transaction_batch_file':
        temp_tbl_name = "fnt.tbl_temp_cof_transactions"
        sql1 =   """
                CREATE TABLE IF NOT EXISTS fnt.tbl_temp_cof_transactions(
                    latestBatchTimestamp DATETIME,
                    latestBatchFileName TEXT,
                    lastTimestampUpdated DATETIME NOT NULL,
                    creditCardLastFour TEXT,
                    surrogateAccountId TEXT NOT NULL,
                    customerId TEXT NOT NULL,
                    transactionEffectiveDate DATE,
                    transactionPostingDate DATE,
                    transactionPostingTime TIME,
                    authorizationDate DATE,
                    transactionCode INTEGER,
                    transactionType INTEGER,
                    transactionDescription TEXT,
                    dbCrIndicator TEXT,
                    transactionReferenceNumber TEXT,
                    internalExternalBrandFlag TEXT,
                    transactionSequenceNumber NUMERIC,
                    authorizationCode TEXT,
                    transactionAmount NUMERIC,
                    transferFlag TEXT,
                    transactionCurrencyCode TEXT,
                    billedCurrencyCode TEXT,
                    conversionRate NUMERIC,
                    merchantStore INTEGER,
                    merchantId TEXT,
                    merchantCategoryCode TEXT,
                    merchantName TEXT,
                    merchantCity TEXT,
                    merchantState TEXT,
                    merchantCountry TEXT,
                    merchantPostalCode TEXT,
                    invoiceNumber TEXT
                );
                """
        sql2 =  """DELETE FROM fnt.tbl_temp_cof_transactions"""
    elif batch_file == 'account_batch_file':
        temp_tbl_name = "fnt.tbl_temp_cof_account_summary"
        sql1 =   """
                CREATE TABLE IF NOT EXISTS fnt.tbl_temp_cof_account_summary(
                    latestBatchTimestamp DATETIME,
                    latestBatchFileName TEXT,
                    lastTimestampUpdated DATETIME NOT NULL,
                    surrogateAccountId TEXT NOT NULL UNIQUE,
                    nextPaymentDueDate DATE,
                    creditLimit NUMERIC,
                    availableCredit NUMERIC,
                    currentBalance NUMERIC,
                    nextStatementDate DATE,
                    lastPaymentDate DATE,
                    lastPaymentAmount NUMERIC,
                    dateLastUpdated DATE
                );
                """
        sql2 =  """DELETE FROM fnt.tbl_temp_cof_account_summary"""
    else:
        logger_function("ERROR: S3 file name does not match required lookup.", type="error")
        sys.exit()

    return sql1, sql2, temp_tbl_name


###### read csv from S3 helper functions ######
# Read single csv file from S3
def pd_read_s3_csv(key, bucket, s3_client=None, **args):
    if s3_client is None:
        s3_client = boto3.client('s3')
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    return pd.read_csv(io.BytesIO(obj['Body'].read()), **args)

# Read multiple csvs from a folder on S3 generated by spark
def pd_read_s3_multiple_csvs(filepath, bucket, s3=None, 
                                 s3_client=None, verbose=False, **args):
    if not filepath.endswith('/'):
        filepath = filepath + '/'  # Add '/' to the end
    if s3_client is None:
        s3_client = boto3.client('s3')
    if s3 is None:
        s3 = boto3.resource('s3')
    s3_keys = [item.key for item in s3.Bucket(bucket).objects.filter(Prefix=filepath)
               if item.key.endswith('.csv')]
    if not s3_keys:
        logger_function(f"ERROR: No csv found in {bucket}{filepath}.", type="error")
        sys.exit()
    elif verbose:
        print('Load csvs:')
        for p in s3_keys: 
            print(p)
    dfs = [pd_read_s3_csv(key, bucket=bucket, s3_client=s3_client, **args) 
           for key in s3_keys]
    return pd.concat(dfs, ignore_index=True)


###### lambda primary function ######
def lambda_handler(event, context):
    """
    This function begins writer from S3 to Postgres

    Args:
        event (object): Event data that's passed to the function upon execution
        context (object): Python objects that implements methods and has attributes
    
    Returns:
        NONE
    """
    # return credentials for connecting to Aurora Postgres
    logger_function("Attempting Aurora Postgres connection...", type="info")
    #TODO update secret name
    credential = get_db_secret(secret_name="auroraPostgres2", region_name="us-west-2")

    # connect to database
    conn = db_connector(credential)
    cursor = conn.cursor()

    ##################################
    ## HANDLED DIRECTLY IN POSTGRES ##
    # (a) create fnt schema if it does not exist
    # (b) create tables by running schema file
    # TODO reference .sql file instead of python function if running this from Lambda
    ##################################

    # (0) Get file(s) from stage
    # TODO use sql file, not python
    try:
        # Read in SNS message
        sns_message = event['Records'][0]['Sns']['Message']

        # Get batch file name
        key_name = json.loads(sns_message)['Records'][0]['s3']['object']['key']
        bucket_name = json.loads(sns_message)['Records'][0]['s3']['bucket']['name']
    except ClientError as e:
        logger_function("ERROR: Could not access S3 file.", type="error")
        logger_function(e, type="error")
        sys.exit()

    # (1) create if not exists temp table in RDS (e.g., tbl_temp_cof_account_master)
    file_name_with_path = contains_keywords(key_name) 
    sql1, sql2, temp_tbl_name = get_temp_table_schema(key_name)
    cursor.execute(sql1)

    # (1b) whether we create a new table or not, need to remove all records as it should be empty
    cursor.execute(sql2)

    # (2) import csvs from S3 into dataframe
    df = pd_read_s3_multiple_csvs(s3_file_path, bucket_name)

    # (3) upload dataframe into sql table
    # TODO use pyspark
    buffer = io.StringIO()
    df.to_csv(buffer, index=False, header=False)
    buffer.seek(0)
    with cursor:
        try:
            cursor.copy_expert(f"COPY {temp_tbl_name} FROM STDIN (FORMAT 'csv', HEADER false)", buffer)
        except (Exception, psycopg2.DatabaseError) as error:
            logger_function("Error: %s" % error, type="error")

    # (4) TODO move temp table data to "operational table":
        #options are using Lambda (bad), using Postgres trigger (better), step functions, glue, etc. (best)

    # closing the connection
    cursor.close()
    conn.close()
    logger_function("SUCCESS: Batch ETL operations complete.", type="info")
    
    return 