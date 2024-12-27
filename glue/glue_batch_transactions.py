"""
This Glue Job imports the batch file from Raw bucket and transforms before sending to stage and uploading to RDS
"""

# packages
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from datetime import datetime
import pandas as pd
import pandas.io.sql as sqlio
import sys
import logging
import psycopg2
from psycopg2.extras import execute_values
import json
import boto3
from botocore.exceptions import ClientError
import io
import numpy as np
pd.options.mode.chained_assignment = None


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
def db_connector(credential, dbname):
    """
    This function creates the connection object for Aurora Postgres

    Args:
        credential (dict): Dictionary containing secret key:value pairs for database connection
        dbname (str): Name of database

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
                                port=rds_port,
                                dbname=dbname)
        conn.autocommit = True
        logger_function("SUCCESS: Connection to Aurora Postgres instance succeeded.", type="info")
    except psycopg2.Error as e:
        logger_function(f"ERROR: Could not connect to Postgres instance: {e}", type="error")
        logger_function(e, type="error")
        sys.exit()

    # return connection object
    return conn


###### temp table function ######
def get_temp_table_schema():
    """
    This function uses the file name uploaded to S3 to identify the temporary table that should be created

    Args:
        NONE

    Returns
        sql1 (string): SQL statement used to create the temp table in Aurora Postgres
        sql2 (string): SQL statement used to delete records from temp table
        temp_tbl_name (string): Name of temporary table
    """

    # TODO - only need temp_table_name and sql2 (DELETE FROM)
    temp_tbl_name = "batch.batch_temp_card_posted_transactions"
    sql0 = """CREATE SCHEMA IF NOT EXISTS batch;"""
    sql1 =   """
            CREATE TABLE IF NOT EXISTS batch.batch_temp_card_posted_transactions(
                cof_account_surrogate_id TEXT NOT NULL,
                cof_customer_surrogate_id TEXT,
                transaction_status TEXT,
                transaction_amount NUMERIC,
                transaction_currency_code TEXT,
                posted_date DATE,
                merchant_name TEXT,
                merchant_category_code TEXT NOT NULL,
                merchant_logo_url TEXT,
                merchant_website TEXT,
                merchant_phone_number TEXT,
                merchant_store_id TEXT,
                merchant_address JSONB,
                created_timestamp TIMESTAMP WITH TIME ZONE,
                updated_timestamp TIMESTAMP WITH TIME ZONE,
                created_by TEXT,
                updated_by TEXT,
                latest_batch_filename TEXT NOT NULL
            );
            """
    sql2 =  """DELETE FROM batch.batch_temp_card_posted_transactions"""

    return sql0, sql1, sql2, temp_tbl_name


###### procedure creation function ######
def create_upsert_procedure(cursor):
    """
    This function creates a stored procedure in PostgreSQL for upserting data 
    into the  card_posted_transactions table.

    Args:
        cursor (object): Database cursor to execute SQL commands
    """

    # Define the SQL for creating the stored procedure
    # TODO - do we want to create or replace or just create if not exists?

    sql_procedure = """
    CREATE OR REPLACE PROCEDURE batch.upsert_temp_to_card_posted_transactions()
    LANGUAGE plpgsql
    AS $$
    BEGIN
        -- Perform the upsert operation from the temporary table to the operational table
        INSERT INTO etltest.card_posted_transactions (
            cof_account_surrogate_id,
            cof_customer_surrogate_id,
            transaction_status,
            transaction_amount,
            transaction_currency_code,
            posted_date,
            merchant_name,
            merchant_category_code,
            merchant_logo_url,
            merchant_website,
            merchant_phone_number,
            merchant_store_id,
            merchant_address,
            created_timestamp,
            updated_timestamp,
            created_by,
            updated_by
        )
        SELECT 
            temp.cof_account_surrogate_id,
            temp.cof_customer_surrogate_id,
            temp.transaction_status,
            temp.transaction_amount,
            temp.transaction_currency_code,
            temp.posted_date,
            temp.merchant_name,
            temp.merchant_category_code,
            temp.merchant_logo_url,
            temp.merchant_website,
            temp.merchant_phone_number,
            temp.merchant_store_id,
            temp.merchant_address,
            temp.created_timestamp,
            temp.updated_timestamp,
            temp.created_by,
            temp.updated_by
        FROM 
            batch.batch_temp_card_posted_transactions temp;

        RAISE NOTICE 'UPSERT operation completed successfully.';
    EXCEPTION
        WHEN OTHERS THEN
            RAISE EXCEPTION 'Error during UPSERT operation: %', SQLERRM;
    END $$;
    """
    # Execute the SQL query to create the stored procedure
    cursor.execute(sql_procedure)


###### validation and partition function ######
def validate_and_partition_df(df, conn):
    """
    Uses queries on df to validate records and partition into processed and rejected dataframes.

    Args:
        df (df): Pandas DataFrame.
        conn (object): Connection object on Aurora Postgres instance

    Returns:
        temp_df (df): Pandas Dataframe with records to be processed in Postgres, in temporary table format
        processed_df (df): Pandas Dataframe with records to be processed in Postgres
        rejected_df (df): Pandas Dataframe with records to be rejected in Postgres
    """
    ### Filter Criteria ###

    # STEP 1: remove records with nulls or incorrect values
    # latest_batch_filename NOT NULL
    # updated_timestamp NOT NULL
    # cof_account_surrogate_id NOT NULL
    # cof_customer_surrogate_id NOT NULL
    # transaction_amount NOT NULL
    # transaction_amount > 0.00
    # merchantCategoryCode NOT NULL
    # posted_date NOT NULL
    # posted_date <= NOW()

    now_var = pd.Timestamp.now()
    processed_df = df.query("latest_batch_filename.notnull() & \
                            updated_timestamp.notnull() & \
                            surrogateAccountId.notnull() & \
                            customerID.notnull()  & \
                            transactionAmount.notnull() & \
                            transactionAmount > 0 & \
                            merchantCategoryCode.notnull() & \
                            transactionPostingDate.notnull() & \
                            transactionPostingDate <= @now_var \
                            ")
    # rejected rows are the records filtered out
    # rejected rows receive generic rejection reason
    processed_idxs = processed_df.index.values.tolist()
    temp_df = df.loc[~df.index.isin(processed_idxs)]
    if len(temp_df) > 0:
        temp_df["rejection_reason"] = "record contains null or incorrect values in one or more required fields"
        rejected_df = temp_df.copy()
        del temp_df
    else:
        # initialize empty dataframe for rejected_df
        rejected_df = pd.DataFrame(columns=df.columns)
        rejected_df["rejection_reason"] = None

    # STEP 2 (optional): remove records that don't have existing cof_account_surrogate_id in card_account_summary
    sql_step2 = """
        SELECT cof_account_surrogate_id, cof_customer_surrogate_id
        FROM etltest.card_account_status;
        """
    # TODO: rows = cursor.fetchall()
    df_cof_ids = sqlio.read_sql_query(sql_step2, conn)
    list_cof_ids = df_cof_ids['cof_account_surrogate_id'].to_list()
    temp_df = processed_df[~processed_df['surrogateAccountId'].isin(list_cof_ids)]
    if len(temp_df) > 0:
        temp_df["rejection_reason"] = "record contains cof_account_surrogate_id not in card_account_status"
        if len(rejected_df) > 0:
            rejected_df = pd.concat([rejected_df, temp_df], sort=False, ignore_index=False)
        else:
            del rejected_df
            rejected_df = temp_df.copy()
        del temp_df
    processed_df = processed_df[processed_df['surrogateAccountId'].isin(list_cof_ids)]

    # STEP 3 (optional): remove records that don't have matching merchant_category_code
    sql_step3 = """
        SELECT merchant_category_code, merchant_category_code_description
        FROM etltest.merchant_categories;
        """
    # TODO: rows = cursor.fetchall()
    df_merchant_category_codes = sqlio.read_sql_query(sql_step3, conn)
    list_mccs = df_merchant_category_codes['merchant_category_code'].to_list()
    temp_df = processed_df[~processed_df['merchantCategoryCode'].isin(list_mccs)]
    if len(temp_df) > 0:
        temp_df["rejection_reason"] = "record does not contain valid merchant_category_code"
        if len(rejected_df) > 0:
            rejected_df = pd.concat([rejected_df, temp_df], sort=False, ignore_index=False)
        else:
            del rejected_df
            rejected_df = temp_df.copy()
        del temp_df
    processed_df = processed_df[processed_df['merchantCategoryCode'].isin(list_mccs)]

    # replace NaN with blanks
    processed_df = processed_df.fillna('')
    rejected_df = rejected_df.fillna('')

    # reformat processed_df as json 
    processed_df_tbl = processed_df.astype(str)
    # TODO removing merchant_address for now - need to figure out how to store nested JSONB
    processed_df_tbl['row_data'] = '{ \
                                "cof_account_surrogate_id": "'+processed_df_tbl['surrogateAccountId']+'", \
                                "cof_customer_surrogate_id": "'+processed_df_tbl['customerID']+'", \
                                "transaction_status": "'+processed_df_tbl['transactionAmount']+'", \
                                "transaction_amount": "'+processed_df_tbl['transactionCurrencyCode']+'", \
                                "transaction_currency_code": "'+processed_df_tbl['transactionPostingDate']+'", \
                                "posted_date": "'+processed_df_tbl['surrogateAccountId']+'", \
                                "merchant_name": "'+processed_df_tbl['merchantName']+'", \
                                "merchant_category_code": "'+processed_df_tbl['merchantCategoryCode']+'", \
                                "merchant_logo_url": "'+processed_df_tbl['merchant_logo_url']+'", \
                                "merchant_website": "'+processed_df_tbl['merchant_website']+'", \
                                "merchant_phone_number": "'+processed_df_tbl['merchant_phone_number']+'", \
                                "merchant_store_id": "'+processed_df_tbl['merchantID']+'", \
                                "created_timestamp": "'+processed_df_tbl['created_timestamp']+'", \
                                "updated_timestamp": "'+processed_df_tbl['updated_timestamp']+'", \
                                "created_by": "'+processed_df_tbl['created_by']+'", \
                                "updated_by": "'+processed_df_tbl['updated_by']+'" \
                                }'

    # Adjust processed_df for sql table format
    processed_df_final_cols = ['latest_batch_filename','row_data']
    processed_df_tbl = processed_df_tbl[processed_df_final_cols]
    process_timestamp = pd.to_datetime(now_var, format="%Y%m%d%H%M%S")
    processed_df_tbl['processed_at'] = process_timestamp

    # reformat rejected_df as json 
    rejected_df_tbl = rejected_df.astype(str)
    # TODO removing merchant_address for now - need to figure out how to store nested JSONB
    rejected_df_tbl['row_data'] = '{ \
                                "cof_account_surrogate_id": "'+rejected_df_tbl['surrogateAccountId']+'", \
                                "cof_customer_surrogate_id": "'+rejected_df_tbl['customerID']+'", \
                                "transaction_status": "'+rejected_df_tbl['transactionAmount']+'", \
                                "transaction_amount": "'+rejected_df_tbl['transactionCurrencyCode']+'", \
                                "transaction_currency_code": "'+rejected_df_tbl['transactionPostingDate']+'", \
                                "posted_date": "'+rejected_df_tbl['surrogateAccountId']+'", \
                                "merchant_name": "'+rejected_df_tbl['merchantName']+'", \
                                "merchant_category_code": "'+rejected_df_tbl['merchantCategoryCode']+'", \
                                "merchant_logo_url": "'+rejected_df_tbl['merchant_logo_url']+'", \
                                "merchant_website": "'+rejected_df_tbl['merchant_website']+'", \
                                "merchant_phone_number": "'+rejected_df_tbl['merchant_phone_number']+'", \
                                "merchant_store_id": "'+rejected_df_tbl['merchantID']+'", \
                                "created_timestamp": "'+rejected_df_tbl['created_timestamp']+'", \
                                "updated_timestamp": "'+rejected_df_tbl['updated_timestamp']+'", \
                                "created_by": "'+rejected_df_tbl['created_by']+'", \
                                "updated_by": "'+rejected_df_tbl['updated_by']+'" \
                                }'

    # Adjust rejected_df for sql table format
    rejected_df_final_cols = ['latest_batch_filename','row_data','rejection_reason']
    rejected_df_tbl = rejected_df_tbl[rejected_df_final_cols]
    rejected_df_tbl['rejected_at'] = process_timestamp
    rejected_df_tbl['is_resolved'] = False
    rejected_df_tbl['comments'] = ''

    rejected_df_tbl_final_cols = ['latest_batch_filename','row_data','rejected_at',
                 'rejection_reason','is_resolved','comments']
    rejected_df_tbl = rejected_df_tbl[rejected_df_tbl_final_cols]

    return processed_df, processed_df_tbl, rejected_df_tbl


###############################################################################
###############################################################################
############################### FUNCTION START ################################
###############################################################################
###############################################################################

# Get command-line arguments
args = getResolvedOptions(sys.argv, 
                          ['JOB_NAME', 
                           'source_key', 
                           'source_bucket', 
                           'dest_bucket',
                           'batch_file_name',
                           'batch_timestamp'])
source_key = args['source_key']
source_bucket = args['source_bucket']
dest_bucket = args['dest_bucket']
batch_file_name = args['batch_file_name']
batch_timestamp = args['batch_timestamp']

# Initialize Spark context, Glue context, and the Glue job
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# initiate logger for CloudWatch
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize a session using Amazon S3
s3 = boto3.client('s3')

# Download the file from S3
response = s3.get_object(Bucket=source_bucket, Key=source_key)
file_content = response['Body'].read()

# Assign column headers based on known schema
column_headers_all = ['recordType','cardNumber','surrogateAccountId','customerID','transactionEffectiveDate','transactionPostingDate','transactionPostingTime','transactionCode','transactionType','transactionDescription', \
                  'dbCrIndicator','transactionReferenceNumber','internalExternalBrandFlag','transactionSequenceNumber','authorizationCode','transactionAmount','transactionCurrencyCode','billedCurrencyCode','conversionRate','merchantStore', \
                  'merchantID','merchantCategoryCode','merchantName','merchantCity','merchantState','merchantCountry','merchantPostalCode','invoiceNumber']

column_headers_keep = ['surrogateAccountId','customerID','transactionAmount','transactionCurrencyCode',
                       'transactionPostingDate','merchantName','merchantCategoryCode','merchantID',
                       'merchantCity','merchantState','merchantCountry','merchantPostalCode']

# Assuming the .dat file is a CSV-like format, read it into a pandas DataFrame
# Adjust the parameters of pd.read_csv() as needed for your specific file format
# Read the file into a pandas DataFrame, skipping the first row and the last row
# Read everything as string, will change data types later
df = pd.read_csv(io.BytesIO(file_content), delimiter='|',skiprows=1, skipfooter=1, engine='python', on_bad_lines='skip', names = column_headers_all, dtype=str, keep_default_na=False)
df = df[column_headers_keep]

# Define the desired data types for each column
dtype_dict = {
    'surrogateAccountId':str,
    'customerID':str,
    'transactionPostingDate':str,
    'transactionAmount':float,
    'transactionCurrencyCode':str,
    'merchantID':str,
    'merchantCategoryCode':str,
    'merchantName':str,
    'merchantCity':str,
    'merchantState':str,
    'merchantCountry':str,
    'merchantPostalCode':str,
}
df = df.astype(dtype_dict)
logger_function("Batch file data types updated in Dataframe.", type="info")

# Reformat dates to YYYY-MM-DD
df['transactionPostingDate'] = pd.to_datetime(df['transactionPostingDate'])

# Format the current date and time as MM-DD-YYYY HH:MM:SS
now = datetime.now()
date_time_str1 = now.strftime("%m-%d-%Y %H:%M:%S")
date_time_str2 = now.strftime("%m-%d-%Y_%H:%M:%S")

batch_timestamp = pd.to_datetime(batch_timestamp, format="%Y%m%d%H%M%S")
# Add batch_timestamp and latestBatchFileName to df
df.insert(loc=0, column='latest_batch_filename', value=batch_file_name)
#df.insert(loc=0, column='LatestBatchTimestamp', value=batch_timestamp)
df.insert(loc=0, column='created_timestamp', value=batch_timestamp)
df.insert(loc=0, column='updated_timestamp', value=batch_timestamp)

# Add created_by and updated_by columns
df['created_by'] = df['surrogateAccountId']
df['updated_by'] = df['surrogateAccountId']

# Add transaction status column
df.insert(loc=0, column='transaction_status', value="Posted")

# Add in null columns
df['merchant_logo_url'] = np.nan
df['merchant_website'] = np.nan
df['merchant_phone_number'] = np.nan

# Round transaction amount
df['transactionAmount'] = np.round(df['transactionAmount'],2)

# Combine address feilds for JSONB object
#'{"addressLine1": "1201 County Rd 581","city": "Wesley Chapel","countrySubdivisionCode": "US-FL","postalCode": "33544","countryCode": "USA"}'::jsonb
df['merchant_address'] = '{"addressLine1": "","city": "'+df['merchantCity']+'","countrySubdivisionCode": "'+df['merchantState']+'","postalCode": "'+df['merchantPostalCode']+'","countryCode": "'+df['merchantCountry']+'"}'

# remove address parts and organize columns
df_final_cols = ['surrogateAccountId','customerID','transaction_status','transactionAmount',
                 'transactionCurrencyCode','transactionPostingDate','merchantName',
                 'merchantCategoryCode','merchant_logo_url',
                 'merchant_website','merchant_phone_number','merchantID','merchant_address',
                 'created_timestamp','updated_timestamp','created_by','updated_by',
                 'latest_batch_filename']
df = df[df_final_cols]
df = df.replace('', np.nan)

# format as csv and save to s3
extension = ".parquet"
s3_prefix = "s3://"
new_file_name = f"{s3_prefix}{dest_bucket}/cof-transactions/cof_staged_transactions_{date_time_str2}.{extension}"

# # Convert Pandas DataFrame to PySpark DataFrame
#print("Converting Pandas to PySpark DF")
#spark_df = spark.createDataFrame(df)

# Write the dataframe to the specified S3 path in CSV format
#try:
#    spark_df.write\
#         .format("parquet")\
#         .option("quote", None)\
#         .option("header", "true")\
#         .mode("append")\
#         .save(new_file_name)
#    logger_function("Batch file saved as parquet in stage bucket.", type="info")
#except Exception as e:
#    raise

# Create json file with job details for subsequent Lambda functions
# TODO parameterize hardcoded key names
result = {}
result['batchType'] = 'Transactions'
result['batchFileName'] = batch_file_name
result['timestamp'] = date_time_str1
result['s3_bucket'] = dest_bucket
result['s3_key'] = f"cof-transactions/cof_staged_transactions_{date_time_str2}.{extension}"
result['my_key'] = f"cof-transactions/cof_staged_transactions_metadata.json"

# Write result json file to S3
print("Writing JSON")
json_obj = json.dumps(result)
s3.put_object(Bucket=dest_bucket, Key=result['my_key'], Body=json_obj)
logger_function("Metadata written to stage bucket.", type="info")
print("JSON Writing successful")

# return credentials for connecting to Aurora Postgres
logger_function("Attempting Aurora Postgres connection...", type="info")
# TODO parameterize hardcoded secret name
credential = get_db_secret(secret_name="rds/dev/fnt/admin", region_name="us-west-2")

# connect to database
dbname = "dev_fnt_rds_card_account_service"
conn = db_connector(credential, dbname)
cursor = conn.cursor()
print("Database: ", conn)

# (1) create if not exists temp table in RDS
print('Creating temporary table')
sql0, sql1, sql2, temp_tbl_name = get_temp_table_schema()
#cursor.execute(sql0) # No need to create schema here
cursor.execute(sql1)

# (2) whether we create a new table or not, need to remove all records as it should be empty
print('Truncating temp table')
cursor.execute(sql2)

# Call the function to create the stored procedure
try:
    create_upsert_procedure(cursor)
except Exception as e:
    logger_function(f"Stored procedure creation for upsert failed: {e}", type="error")

# (3) Split batch file into processed and rejected dataframes based on validations
temp_df, processed_df, rejected_df = validate_and_partition_df(df, conn)

###############################################################################
############################### SQL COPY COMMANDS #############################
###############################################################################

# TODO TEMPORARY SOLUTION - RECONNECT as we are losing connection object after each copy

# TODO use pyspark
# (4a) Upload processed_df (append)
try:
    buffer = io.StringIO()
    processed_df.to_csv(buffer, index=False, header=False)
except Exception as e:
    logger_function(f"writing df to csv failed: {e}", type="error")
buffer.seek(0)
with cursor:
    try:
        print("Copying csv to processed_df table")
        processed_df_tbl_name = 'batch.processed_data_log_transactions'
        processed_df_tbl_cols = '(batch_file_name,row_data,processed_at)'
        cursor.copy_expert(f"COPY {processed_df_tbl_name}{processed_df_tbl_cols} FROM STDIN (FORMAT 'csv', HEADER false)", buffer)
        conn.commit()
    except (Exception, psycopg2.DatabaseError) as error:
        logger_function("Error: %s" % error, type="error")

# (4b) Upload rejected_df (append)
conn = db_connector(credential, dbname)
cursor = conn.cursor()
try:
    buffer = io.StringIO()
    rejected_df.to_csv(buffer, index=False, header=False)
except Exception as e:
    logger_function(f"writing df to csv failed: {e}", type="error")
buffer.seek(0)
with cursor:
    try:
        print("Copying csv to rejected_df table")
        rejected_df_tbl_name = 'batch.rejected_data_log_transactions'
        rejected_df_tbl_cols = '(batch_file_name,row_data,rejected_at,rejection_reason,is_resolved,comments)'
        cursor.copy_expert(f"COPY {rejected_df_tbl_name}{rejected_df_tbl_cols} FROM STDIN (FORMAT 'csv', HEADER false)", buffer)
        conn.commit()
    except (Exception, psycopg2.DatabaseError) as error:
        logger_function("Error: %s" % error, type="error")

# (4c) Upload temp_df (replace)
conn = db_connector(credential, dbname)
cursor = conn.cursor()
try:
    buffer = io.StringIO()
    temp_df.to_csv(buffer, index=False, header=False)
except Exception as e:
    logger_function(f"writing df to csv failed: {e}", type="error")
buffer.seek(0)
with cursor:
    try:
        print("Copying csv to temp table")
        cursor.copy_expert(f"COPY {temp_tbl_name} FROM STDIN (FORMAT 'csv', HEADER false)", buffer)
        conn.commit()
    except (Exception, psycopg2.DatabaseError) as error:
        logger_function("Error: %s" % error, type="error")

# (4d) Call upsert
conn = db_connector(credential, dbname)
cursor = conn.cursor()
print('Upserting')
with cursor:
    try:
        cursor.execute("CALL batch.upsert_temp_to_card_posted_transactions();")
        conn.commit()
    except (Exception, psycopg2.DatabaseError) as error:
        logger_function("Error: %s" % error, type="error")

# (5) Close connection and commit Glue Job
cursor.close()
conn.close()
logger_function("Batch file copied to RDS.", type="info")

# Commit the Glue job
job.commit()