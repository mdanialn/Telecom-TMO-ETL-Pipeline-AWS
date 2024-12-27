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

    temp_tbl_name = "fnt.batch_temp_application"
    sql0 = """CREATE SCHEMA IF NOT EXISTS fnt;"""

    #TODO columns/fields for batch_temp_application need to be determined 
    sql1 =   """
            CREATE TABLE IF NOT EXISTS fnt.batch_temp_application(
               
            );
            """
    sql2 =  """DELETE FROM fnt.batch_temp_application"""

    return sql0, sql1, sql2, temp_tbl_name

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
column_headers_all = ['RecordType','brand','Accounttype','Applicationidentifier', 'ApplicationDate','Channeltype','Merchant/StoreNumber','ApplicationDecision',
                  'SurrogateAccountId','SubChannel','DigitalEntryChannel','RegisterID','AssociateID',
                  'OfferType']

#TODO which columns need to be kept                  
column_headers_keep = []

# Assuming the .dat file is a CSV-like format, read it into a pandas DataFrame
# Adjust the parameters of pd.read_csv() as needed for your specific file format
# Read the file into a pandas DataFrame, skipping the first row and the last row
# Read everything as string, will change data types later
logger_function("Attempting to read batch file...", type="info")
df = pd.read_csv(io.BytesIO(file_content), delimiter=',',skiprows=1, skipfooter=1, engine='python', on_bad_lines='skip', names = column_headers_all, dtype=str)
df = df[column_headers_keep]

#TODO Define the desired data types for each column
dtype_dict = {
    
}
df = df.astype(dtype_dict)
logger_function("Batch file data types updated in Dataframe.", type="info")

# Reformat dates to YYYY-MM-DD
df['NextPaymentDueDate'] = pd.to_datetime(df['NextPaymentDueDate'], format="%Y-%m-%d")
df['NextStatementDate'] = pd.to_datetime(df['NextStatementDate'], format="%Y-%m-%d")
df['LastPaymentDate'] = pd.to_datetime(df['LastPaymentDate'], format="%Y-%m-%d")
df['DateLastUpdated'] = pd.to_datetime(df['DateLastUpdated'], format="%Y-%m-%d")

# Format the current date and time as MM-DD-YYYY HH:MM:SS
now = datetime.now()
date_time_str1 = now.strftime("%Y-%m-%d %H:%M:%S")
date_time_str2 = now.strftime("%Y-%m-%d_%H:%M:%S")

# Add datetime to df
df.insert(loc=0, column='LastTimestampUpdated', value=date_time_str1)

# Add latestBatchTimestamp and latestBatchFileName to df
df.insert(loc=0, column='LatestBatchTimestamp', value=batch_timestamp)
df.insert(loc=0, column='LatestBatchFileName', value=batch_file_name)

# format as parquet and save to s3
extension = ".parquet"
s3_prefix = "s3://"
new_file_name = f"{s3_prefix}{dest_bucket}/cof-account-master/cof_staged_account_master_{date_time_str2}.{extension}"

# Convert Pandas DataFrame to PySpark DataFrame
spark_df = spark.createDataFrame(df)

# Write the dataframe to the specified S3 path in CSV format
spark_df.write\
     .format("parquet")\
     .option("quote", None)\
     .option("header", "true")\
     .mode("append")\
     .save(new_file_name)
logger_function("Batch file saved as parquet in stage bucket.", type="info")

# Create json file with job details for subsequent Lambda functions
# TODO parameterize hardcoded key names
result = {}
result['batchType'] = 'application'
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
credential = get_db_secret(secret_name="rds/dev/fnt/batch_user1", region_name="us-west-2")

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

# (3) upload dataframe into sql table
#TODO use pyspark
buffer = io.StringIO()
df.to_csv(buffer, index=False, header=False)
buffer.seek(0)
with cursor:
    try:
        
        cursor.copy_expert(f"COPY {temp_tbl_name} FROM STDIN (FORMAT 'csv', HEADER false)", buffer)

        # Trigger upsert stored procedure
        cursor.execute("CALL upsert_dummy_application();")
        conn.commit()
    except (Exception, psycopg2.DatabaseError) as error:
        logger_function("Error: %s" % error, type="error")

# closing the connection
cursor.close()
conn.close()
logger_function("Batch file copied to RDS.", type="info")

# Commit the Glue job
job.commit()