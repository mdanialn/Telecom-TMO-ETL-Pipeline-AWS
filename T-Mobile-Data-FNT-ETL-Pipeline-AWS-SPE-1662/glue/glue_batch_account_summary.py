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
import sys
import logging
import psycopg2
import json
import boto3
from botocore.exceptions import ClientError
import io


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
        logger_function("ERROR: Could not connect to Postgres instance.", type="error")
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

    temp_tbl_name = "batch.batch_temp_card_account_summary"
    sql0 = """CREATE SCHEMA IF NOT EXISTS batch;"""
    sql1 =   """
            CREATE TABLE IF NOT EXISTS batch.batch_temp_card_account_summary(
                credit_card_last_four varchar(4),
                tmo_uuid TEXT,
                latest_batch_timestamp timestamptz,
                latest_batch_filename TEXT,
                last_updated_timestamp timestamptz NOT NULL,
                cof_account_surrogate_id  TEXT NOT NULL PRIMARY KEY,
                next_payment_due_Date DATE,
                credit_limit NUMERIC,
                available_credit NUMERIC,
                current_balance NUMERIC,
                next_statement_date DATE,
                last_payment_date DATE,
                last_payment_amount NUMERIC,
                date_last_updated DATE,
                billing_cycle_day INTEGER
            );
            """
    sql2 =  """DELETE FROM batch.batch_temp_card_account_summary"""

    return sql0, sql1, sql2, temp_tbl_name

###### procedure creation function ######
def create_upsert_procedure(cursor):
    """
    This function creates a stored procedure in PostgreSQL for upserting data 
    into the dummy_card_account_summary table.

    Args:
        cursor (object): Database cursor to execute SQL commands
    """
    # Define the SQL for creating the stored procedure
    sql_procedure = """
    CREATE OR REPLACE PROCEDURE batch.upsert_temp_to_operational_card_account_summary()
    LANGUAGE plpgsql
    AS $$
    BEGIN
        
        -- Perform the upsert operation from the temporary table to the operational table
        INSERT INTO etltest.card_account_summary_test (
            credit_card_last_four,
            tmo_uuid, 
            cof_account_surrogate_id, 
            next_payment_due_date, 
            credit_limit, 
            available_credit, 
            current_balance, 
            statement_date, 
            last_payment_date, 
            last_payment_amount,
            created_timestamp,
            updated_timestamp
        )
        SELECT
            temp.credit_card_last_four,
            temp.tmo_uuid,  -- Use the joined tmo_uuid
            temp.cof_account_surrogate_id,
            temp.next_payment_due_date, 
            temp.credit_limit, 
            temp.available_credit, 
            temp.current_balance, 
            temp.next_statement_date, 
            temp.last_payment_date, 
            temp.last_payment_amount, 
            temp.latest_batch_timestamp,
            temp.last_updated_timestamp
    FROM 
        batch.batch_temp_card_account_summary temp
        ON CONFLICT (cof_account_surrogate_id) 
        DO UPDATE 
        SET 
            credit_card_last_four =EXCLUDED.credit_card_last_four,
            tmo_uuid = EXCLUDED.tmo_uuid,
            cof_account_surrogate_id = EXCLUDED.cof_account_surrogate_id,
            next_payment_due_date = EXCLUDED.next_payment_due_date,
            credit_limit = EXCLUDED.credit_limit,
            available_credit = EXCLUDED.available_credit,
            current_balance = EXCLUDED.current_balance,
            statement_date = EXCLUDED.statement_date,
            last_payment_date = EXCLUDED.last_payment_date,
            last_payment_amount = EXCLUDED.last_payment_amount,
            updated_timestamp = EXCLUDED.updated_timestamp
        WHERE etltest.card_account_summary_test.updated_timestamp < EXCLUDED.updated_timestamp;

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
    # LatestBatchFileName NOT NULL
    # LastUpdatedTimestamp NOT NULL
    # cof_account_surrogate_id NOT NULL
    # CreditLimit NOT NULL and greater than or equal to zero
    # AvailableCredit NOT NULL and greater than or equal to zero
    # CurrentBalance NOT NULL and greater than or equal to zero
    # LastPaymentAmount NOT NULL and greater than or equal to zero
    # DateLastUpdated NOT NULL
    # BillingCycleDay is between 1 and 31

    now_var = pd.Timestamp.now()
    processed_df = df.query("LatestBatchFileName.notnull() & \
                            LastUpdatedTimestamp.notnull() & \
                            SurrogateAccountID.notnull() & \
                            CreditLimit.notnull() & \
                            CreditLimit >= 0 & \
                            AvailableCredit.notnull() & \
                            AvailableCredit >= 0 & \
                            CurrentBalance.notnull() & \
                            CurrentBalance >= 0 & \
                            LastPaymentAmount.notnull() & \
                            LastPaymentAmount > 0 & \
                            DateLastUpdated.notnull() & \
                            BillingCycleDay > 0 & \
                            BillingCycleDay < 32 \
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

    # replace NaN with blanks
    processed_df = processed_df.fillna('')
    rejected_df = rejected_df.fillna('')

    # reformat processed_df as json 
    processed_df_tbl = processed_df.astype(str)
    processed_df_tbl['row_data'] = '{ \
                                "credit_card_last_four": "'+processed_df_tbl['CreditCardLastFour']+'", \
                                "tmo_uuid": "'+processed_df_tbl['tmoUUID']+'", \
                                "latest_batch_timestamp": "'+processed_df_tbl['LatestBatchTimestamp']+'", \
                                "latest_batch_filename": "'+processed_df_tbl['LatestBatchFileName']+'", \
                                "last_updated_timestamp": "'+processed_df_tbl['LastUpdatedTimestamp']+'", \
                                "cof_account_surrogate_id": "'+processed_df_tbl['SurrogateAccountID']+'", \
                                "next_payment_due_Date": "'+processed_df_tbl['NextPaymentDueDate']+'", \
                                "credit_limit": "'+processed_df_tbl['CreditLimit']+'", \
                                "available_credit": "'+processed_df_tbl['AvailableCredit']+'", \
                                "current_balance": "'+processed_df_tbl['CurrentBalance']+'", \
                                "next_statement_date": "'+processed_df_tbl['NextStatementDate']+'", \
                                "last_payment_date": "'+processed_df_tbl['LastPaymentDate']+'", \
                                "last_payment_amount": "'+processed_df_tbl['LastPaymentAmount']+'", \
                                "date_last_updated": "'+processed_df_tbl['DateLastUpdated']+'", \
                                "billing_cycle_day": "'+processed_df_tbl['BillingCycleDay']+'" \
                                }'
    # processed_df_tbl['row_data'] = processed_df_tbl.apply(lambda r: r.to_json())

    # Adjust processed_df for sql table format
    processed_df_final_cols = ['LatestBatchFileName','row_data']
    processed_df_tbl = processed_df_tbl[processed_df_final_cols]
    process_timestamp = pd.to_datetime(now_var, format="%Y%m%d%H%M%S")
    processed_df_tbl['processed_at'] = process_timestamp

    # reformat rejected_df as json 
    rejected_df_tbl = rejected_df.astype(str)
    rejected_df_tbl['row_data'] = '{ \
                                "credit_card_last_four": "'+rejected_df_tbl['CreditCardLastFour']+'", \
                                "tmo_uuid": "'+rejected_df_tbl['tmoUUID']+'", \
                                "latest_batch_timestamp": "'+rejected_df_tbl['LatestBatchTimestamp']+'", \
                                "latest_batch_filename": "'+rejected_df_tbl['LatestBatchFileName']+'", \
                                "last_updated_timestamp": "'+rejected_df_tbl['LastUpdatedTimestamp']+'", \
                                "cof_account_surrogate_id": "'+rejected_df_tbl['SurrogateAccountID']+'", \
                                "next_payment_due_Date": "'+rejected_df_tbl['NextPaymentDueDate']+'", \
                                "credit_limit": "'+rejected_df_tbl['CreditLimit']+'", \
                                "available_credit": "'+rejected_df_tbl['AvailableCredit']+'", \
                                "current_balance": "'+rejected_df_tbl['CurrentBalance']+'", \
                                "next_statement_date": "'+rejected_df_tbl['NextStatementDate']+'", \
                                "last_payment_date": "'+rejected_df_tbl['LastPaymentDate']+'", \
                                "last_payment_amount": "'+rejected_df_tbl['LastPaymentAmount']+'", \
                                "date_last_updated": "'+rejected_df_tbl['DateLastUpdated']+'", \
                                "billing_cycle_day": "'+rejected_df_tbl['BillingCycleDay']+'" \
                                }'
    # rejected_df_tbl['row_data'] = rejected_df_tbl.apply(lambda r: r.to_json())

    # Adjust rejected_df for sql table format
    rejected_df_final_cols = ['LatestBatchFileName','row_data','rejection_reason']
    rejected_df_tbl = rejected_df_tbl[rejected_df_final_cols]
    rejected_df_tbl['rejected_at'] = process_timestamp
    rejected_df_tbl['is_resolved'] = False
    rejected_df_tbl['comments'] = ''

    rejected_df_tbl_final_cols = ['LatestBatchFileName','row_data','rejected_at',
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
column_headers_all = ['RecordType','brand','SurrogateAccountID','Privacymail','PrivacyDateMail','PrivacyEmail','PrivacyDateEmail','PrivacyPhone',
                  'PrivacyDatePhone','GLBFlag','AccountType','BankruptcyIndicator',
                  'AccountClosedIndicator','AccountClosedReason','FraudIndicator','HardshipIndicator',
                  'DeceasedIndicator','ChargeoffIndicator','PotentialFraudIndicator',
                  'LostStolenIndicator','BadAddressFlag','PaymentCycleDue','AccountOpenState',
                  'WaiveInterest','WaiveLateFees','NumberofLTDNSFOccurrences','NumberofDisputedTransactions',
                  'AmountinDispute','NumberofUnblockedCardholders','BillingCycleDay','DateLastUpdated','CreditLimit',
                  'CreditLimitDateChange','CashLimit','ProduceStatement','ExecutiveResolutionAccount', 'CurrentBalance','AvailableCredit','NumberofCTDpurchases',
                  'NumberofCTDreturns','AmountofCTDpurchases','AmountofCTDreturns','NumberofYTDpurchases',
                  'NumberofYTDreturns','NumberofYTDpayments','AmountofYTDpurchases','AmountofYTDreturns',
                  'AmountofYTDpayments','NumberofLTDpurchases','NumberofLTDreturns','NumberofLTDpayments',
                  'AmountofLTDpurchases','AmountofLTDreturns','AmountofLTDpayments','HighBalance',
                  'HighBalanceDate','FixedPaymentIndicator','FixedPaymentAmount',
                  'NextPaymentDueDate','LastPaymentDate','LastPaymentAmount','LastPurchaseDate','LastPurchaseAmount',
                  'FirstAuthorizationDate','FirstTransactionDate','NextStatementDate','LanguageIndicator','DaysDelinquency','ActivitySinceOpenIndicator','PaperlessStatementIndicator','AccountClosedDate']

column_headers_keep = ['SurrogateAccountID','NextPaymentDueDate', 'CreditLimit', 'AvailableCredit',
                       'CurrentBalance', 'NextStatementDate', 'LastPaymentDate', 'LastPaymentAmount', 'DateLastUpdated', 'BillingCycleDay']


# Assuming the .dat file is a CSV-like format, read it into a pandas DataFrame
# Adjust the parameters of pd.read_csv() as needed for your specific file format
# Read the file into a pandas DataFrame, skipping the first row and the last row
# Read everything as string, will change data types later
logger_function("Attempting to read batch file...", type="info")
df = pd.read_csv(io.BytesIO(file_content), delimiter='|',skiprows=1, skipfooter=1, engine='python', on_bad_lines='skip', names = column_headers_all, dtype=str)
df = df[column_headers_keep]


# Define the desired data types for each column
dtype_dict = {
    'SurrogateAccountID':str,
    'NextPaymentDueDate':str,
    'CreditLimit':float,
    'AvailableCredit':float,
    'CurrentBalance':float,
    'NextStatementDate':str,
    'LastPaymentDate':str,
    'LastPaymentAmount':float,
    'DateLastUpdated':str,
    'BillingCycleDay':int
}
df = df.astype(dtype_dict)
logger_function("Batch file data types updated in Dataframe.", type="info")

# Reformat dates to YYYY-MM-DD
df['NextPaymentDueDate'] = pd.to_datetime(df['NextPaymentDueDate'], format="%Y%m%d")
df['NextStatementDate'] = pd.to_datetime(df['NextStatementDate'], format="%Y%m%d")
df['LastPaymentDate'] = pd.to_datetime(df['LastPaymentDate'], format="%Y%m%d")
df['DateLastUpdated'] = pd.to_datetime(df['DateLastUpdated'], format="%Y%m%d")

# Format the current date and time as MM-DD-YYYY HH:MM:SS
now = datetime.now()
date_time_str1 = now.strftime("%m-%d-%Y %H:%M:%S")
date_time_str2 = now.strftime("%m-%d-%Y_%H:%M:%S")

batch_timestamp = pd.to_datetime(batch_timestamp, format="%Y%m%d%H%M%S")
# Add batch_timestamp and latestBatchFileName to df
df.insert(loc=0, column='LastUpdatedTimestamp', value=batch_timestamp)
df.insert(loc=0, column='LatestBatchFileName', value=batch_file_name)
df.insert(loc=0, column='LatestBatchTimestamp', value=batch_timestamp)

# Add tmo_uuid column with empty values to match the table schema
df.insert(loc=0, column='tmoUUID', value=None)  
df.insert(loc=0, column='CreditCardLastFour', value=None)

# # format as parquet and save to s3
extension = ".parquet"
s3_prefix = "s3://"
new_file_name = f"{s3_prefix}{dest_bucket}/cof-account-master/cof_staged_account_master_{date_time_str2}.{extension}"

# Convert Pandas DataFrame to PySpark DataFrame
# spark_df = spark.createDataFrame(df)

# # Write the dataframe to the specified S3 path in CSV format
# try:
#     spark_df.write\
#          .format("parquet")\
#          .option("quote", None)\
#          .option("header", "true")\
#          .mode("append")\
#          .save(new_file_name)
#     logger_function("Batch file saved as parquet in stage bucket.", type="info")
# except Exception as e:
#     raise
    

# Create json file with job details for subsequent Lambda functions
# TODO parameterize hardcoded key names
result = {}
result['batchType'] = 'Account Summary'
result['batchFileName'] = batch_file_name
result['timestamp'] = date_time_str1
result['s3_bucket'] = dest_bucket
result['s3_key'] = f"cof-account-master/cof_staged_account_master_{date_time_str2}.{extension}"
result['my_key'] = f"cof-account-master/cof_staged_account_master_metadata.json"

# Write json file to S3
json_obj = json.dumps(result)
s3.put_object(Bucket=dest_bucket, Key=result['my_key'], Body=json_obj)
logger_function("Metadate written to stage bucket.", type="info")

# return credentials for connecting to Aurora Postgres
logger_function("Attempting Aurora Postgres connection...", type="info")
#TODO parameterize hardcoded secret name
credential = get_db_secret(secret_name="rds/dev/fnt/admin", region_name="us-west-2")

# connect to database
dbname = "dev_fnt_rds_card_account_service"
conn = db_connector(credential, dbname)
cursor = conn.cursor()

# (1) create if not exists temp table in RDS (e.g., tbl_temp_cof_account_master)
sql0, sql1, sql2, temp_tbl_name = get_temp_table_schema()
cursor.execute(sql0)
cursor.execute(sql1)

# (2) whether we create a new table or not, need to remove all records as it should be empty
cursor.execute(sql2)

# Call the function to create the stored procedure
try:
    create_upsert_procedure(cursor)
except Exception as e:
    logger_function("stored procedure creation for upsert failed", type="error")


# (3) Split batch file into processed and rejected dataframes based on validations
temp_df, processed_df, rejected_df = validate_and_partition_df(df, conn)

print(processed_df)

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
    logger_function(f"writing processed_df to csv failed: {e}", type="error")
buffer.seek(0)
with cursor:
    try:
        print("Copying csv to processed_df table")
        processed_df_tbl_name = 'batch.processed_data_log_account_summary'
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
    logger_function(f"writing rejected_df to csv failed: {e}", type="error")
buffer.seek(0)
with cursor:
    try:
        print("Copying csv to rejected_df table")
        rejected_df_tbl_name = 'batch.rejected_data_log_account_summary'
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

#Populate tmo_uuid and credit_card_last_four in the temporary table
conn = db_connector(credential, dbname)
cursor = conn.cursor()
try:
    update_query = """
        -- Populate tmo_uuid  and caredit_card_last_four in the temporary table by joining with card_account_status
        UPDATE batch.batch_temp_card_account_summary temp
        SET 
            tmo_uuid = status.tmo_uuid,
            credit_card_last_four =status.cof_primary_card_last_four
            
        FROM etltest.card_account_status status
        WHERE temp.cof_account_surrogate_id = status.cof_account_surrogate_id;
    """
    cursor.execute(update_query)
    conn.commit()
    logger_function("tmo_uuid and credit_card_last_four populated in temporary table successfully.", type="info")
except Exception as e:
    logger_function(f"Error populating tmo_uuid and credit_card_last_four in temp table: {e}", type="error")
    raise

conn = db_connector(credential, dbname)
cursor = conn.cursor()
# Call the upsert stored procedure to upsert data from temp table to operational table
try:
    logger_function("Calling stored procedure to upsert data into operational table.", type="info")
    cursor.execute("CALL batch.upsert_temp_to_operational_card_account_summary();")
    conn.commit()
    logger_function("Upsert operation completed successfully.", type="info")
except Exception as error:
    logger_function(f"Error during upsert operation: {error}", type="error")


# Close connections
try:
    cursor.close()
    conn.close()
    logger_function("Database connections closed successfully.", type="info")
except Exception as error:
    logger_function(f"Error closing database connections: {error}", type="error")

# Commit the Glue job
try:
    job.commit()
    logger_function("Glue job committed successfully.", type="info")
except Exception as error:
    logger_function(f"Error committing Glue job: {error}", type="error")

