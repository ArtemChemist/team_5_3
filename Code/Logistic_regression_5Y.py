# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC ## Technical notebook to explore autocorrelations in the timesries data

# COMMAND ----------

# importing custom functions
from Code.funcs import blob_connect, write_parquet_to_blob
import csv
import json

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from pyspark.sql.window import Window
from pyspark.sql.types import IntegerType, FloatType, DoubleType,  ArrayType, StringType
from pyspark.sql.functions import size, to_timestamp, mean as _mean, stddev as _stddev, col, sum as _sum, rand, when, collect_list, udf, date_trunc, count, lag, first, last, percent_rank, array
from pyspark.ml.feature import StringIndexer, OneHotEncoder, VectorAssembler, StandardScaler, IndexToString, StringIndexerModel
from pyspark.ml import Pipeline
from pyspark.ml.classification import LogisticRegression as LR

team_blob_url = blob_connect()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read from storage

# COMMAND ----------

# read in daily weather data from parquet

joined = spark.read.parquet(f"{team_blob_url}/ES/new_joins/5Y")

# COMMAND ----------

joined.dtypes

# COMMAND ----------

sig_num =['origin_DailySnowfall', 'origin_6Hr_Precipitation', 'origin_12Hr_Precipitation',]

aux_features = ['sched_depart_date_time_UTC', 'TAIL_NUM', 'DEP_DELAY', 'ORIGIN', 'OP_UNIQUE_CARRIER']

# COMMAND ----------

# take only columns needed
# FIlter out all cancelled
df_clean = joined[aux_features + sig_num].dropna()


# COMMAND ----------

df_clean.count()

# COMMAND ----------

df_clean = df_clean.withColumn('sched_depart_date_time_UTC', to_timestamp(col('sched_depart_date_time_UTC')))
df_clean = df_clean.withColumn('DEP_DELAY', df_clean.DEP_DELAY.cast("float"))

# COMMAND ----------

# MAGIC %md
# MAGIC # Transform

# COMMAND ----------

# MAGIC %md
# MAGIC ### New engineered features

# COMMAND ----------

# Make new column with time in seconds since the begining of Unix epoch
df_clean = df_clean.withColumn('time_long', df_clean.sched_depart_date_time_UTC.cast("long")).orderBy(df_clean.sched_depart_date_time_UTC)

#Helper function to navigate this column
hours = lambda i: i * 3600

# previos delay for this tail number
Time_Tail_Window = Window.partitionBy('TAIL_NUM').orderBy(col('time_long')).rangeBetween(-hours(26), -hours(2))
# average delay for this airline
Time_Carrier_Window = Window.partitionBy('OP_UNIQUE_CARRIER').orderBy(col('time_long')).rangeBetween(-hours(6), -hours(2))
# average delay for this airport
Time_Origin_Window = Window.partitionBy('ORIGIN').orderBy(col('time_long')).rangeBetween(-hours(6), -hours(2))

df_clean = df_clean.withColumns({
    "Av_airport_delay": _mean("DEP_DELAY").over(Time_Origin_Window)
    ,"Prev_delay": last("DEP_DELAY").over(Time_Tail_Window)
    ,"Av_carrier_delay": _mean("DEP_DELAY").over(Time_Carrier_Window)
    ,"Num_flights": count(col("DEP_DELAY")).over(Time_Carrier_Window)
    }).fillna(0)

# COMMAND ----------

df_clean.dtypes

# COMMAND ----------

# MAGIC %md
# MAGIC ### Assemble all features in one vector

# COMMAND ----------

numerical_features = sig_num + ["Av_airport_delay", "Prev_delay", "Av_carrier_delay", "Num_flights"]
input_cols = numerical_features
assembler = VectorAssembler().setInputCols(input_cols).setOutputCol('feat_vec')

# COMMAND ----------

# MAGIC %md
# MAGIC ### Scale features to assist in regularization.

# COMMAND ----------

scaler = StandardScaler().setInputCol('feat_vec').setOutputCol('feat_scaled')

# COMMAND ----------

# MAGIC %md
# MAGIC ### Make Labels column

# COMMAND ----------

# Make label column
df_clean = df_clean.withColumn('IsDelayed',  when(col("DEP_DELAY") >=15, 'Delayed').otherwise('On time'))
lbl_indexer = StringIndexer().setInputCol('IsDelayed').setOutputCol('label')

# COMMAND ----------

# MAGIC %md
# MAGIC ### Assemble and run transform pipeline

# COMMAND ----------

pipeline = Pipeline(stages=[assembler, scaler, lbl_indexer])
df_clean = pipeline.fit(df_clean).transform(df_clean)

# Generate a human-readable list of features
# Read label maps for categorical features
lbl_map = {c.name: c.metadata["ml_attr"]["vals"] for c in df_clean.schema.fields if c.name.endswith("_idx")}

# Assemble formatted list of these features
lbl_lst = []
lbl_base_dic = {}
lbl_idx = 0
'''
for feature in categorical_features:
    for key, value in lbl_map.items():
        if feature == key[:-4]:
            for level in value[:-1]:
                lbl_lst.append(f'{lbl_idx}: {feature} = {level}')
                lbl_idx +=1
            lbl_base_dic[feature] = value[-1]
'''

# Add numerical features to this list features
for ft in numerical_features:
    lbl_lst.append(f'{lbl_idx}: {ft}')
    lbl_idx +=1

# COMMAND ----------

'''
# Write features needed for training to blob
location = 'ES/for_training/1Y_eng'
features_fl = '../Data/1Y_base_features_eng.jsonY_features_eng.csv'
base_features_fl = '../Data/1Y_base_features_eng.json'
write_parquet_to_blob(df_clean[['sched_depart_date_time_UTC','feat_scaled', 'label']], location)


with open(features_fl, 'w', newline='') as file:
    # Step 4: Using csv.writer to write the list to the CSV file
    writer = csv.writer(file)
    writer.writerow(lbl_lst)

with open(base_features_fl, 'w') as fp:
    json.dump(lbl_base_dic, fp)
'''


# COMMAND ----------

'''
#read them back from blob
df_clean = spark.read.parquet(f"{team_blob_url}/{location}")

# Read the list of features from file
lbl_lst = []
with open(features_fl, newline='') as csvfile:
    reader = csv.reader(csvfile, delimiter=',')
    for row in reader:
        lbl_lst = row

with open(base_features_fl, 'r') as j:
     lbl_base_dic = json.loads(j.read())
'''

# COMMAND ----------

# MAGIC %md
# MAGIC #Train and Evaluate

# COMMAND ----------

# MAGIC %md
# MAGIC ### Train / Test Split

# COMMAND ----------

# Generate block boundaries
num_blocks = 5 #number of blocks
split_ratio = 0.8

test_area = 1- (1-split_ratio) * 1/num_blocks
train_block = (1-split_ratio) * 1/num_blocks
train_blocks_boundaries = [(test_area*i/num_blocks, test_area*(i+1)/num_blocks) for i in range(num_blocks)]
test_blocks_boundaries = [(test_block[1], test_block[1] + train_block ) for test_block in train_blocks_boundaries]
print(train_blocks_boundaries)
print(test_blocks_boundaries)

#Create rank column that ranks records by date, from 0 to 1
Rank_Window = Window.partitionBy().orderBy("sched_depart_date_time_UTC")
df_clean = df_clean.withColumn("rank", percent_rank().over(Rank_Window))

# Assemble tuples of train and test datasets for cross-validations
test_train_sets = []
for train_b, test_b in zip(train_blocks_boundaries, test_blocks_boundaries):
    test_train_sets.append((
                            df_clean.where(f"rank <= {train_b[1]} and rank > {train_b[0]}").drop('rank', 'IsDelayed')
                            , df_clean.where(f"rank > {test_b[0]} and rank <= {test_b[1]}").drop('rank', 'IsDelayed')
                            ))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Balance the train dataset

# COMMAND ----------

'''
# Calculate the imbalce
on_time_count = train_df.filter(col('label') < 1).count()
delay_count = train_df.filter(col('label') > 0).count()
on_time_correction = delay_count/on_time_count


# Fix the imbalance on the train_df by dropping ~80% of random on-time flights
train_df = train_df.withColumn('to_keep', when(
      ((rand(seed = 42) < on_time_correction) & (col('label') < 1)) | (col('label') > 0)
      , True   ))

train_df = train_df.filter(train_df.to_keep)

# Confirm the imbalnce is fixed
on_time_count = train_df.filter(col('label') < 1).count()
delay_count = train_df.filter(col('label') > 0).count()
print(f'On Time:{on_time_count}, Delayed: {delay_count}')
'''

# COMMAND ----------

# MAGIC %md
# MAGIC ##Training

# COMMAND ----------

# Create an object model that is heavily biased toward LASSO regularization
lrs = []
models = []
predictions = []
for train_df, test_df in test_train_sets:
    lrs.append(LR(featuresCol='feat_scaled', labelCol='label', maxIter=10, regParam=0.01, elasticNetParam=1))
    models.append(lrs[-1].fit(train_df))
    predictions.append(models[-1].transform(test_df))
# predictions.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Make predictions at various thresholds of what delay odds are considered a predicted delay.

# COMMAND ----------

# Combine predictions from all blocks into a single dataframe
prediction = predictions[0]
for p in predictions[1:]:
    prediction = prediction.union(p)
prediction.count()

# COMMAND ----------

# Convert probability output column to a column with probability of positive

def extract_prob(v):
    try:
        return float(v[1])
    except ValueError:
        return None

extract_prob_udf = udf(extract_prob, DoubleType())
prediction = prediction.withColumn("prob_pos", extract_prob_udf(col("probability")))

# COMMAND ----------

# Set decison cut offs
CutOffs = [0, 0.15, 0.20, 0.25, 0.30, 0.40, 0.60, 0.80]

# Define functions to labeling a prediction as FP(TP) 
# Based on teh cut off
def TP(prob_pos, label):
    return [ 1 if (prob_pos >= cut_off) and (label > 0)  else 0 for cut_off in CutOffs]
def FP(prob_pos, label):
    return [ 1 if (prob_pos >= cut_off) and (label < 1)  else 0 for cut_off in CutOffs]

# Define udfs based on these functions
# These udfs return arrays of the same length as the cut-off array
# With 1 if the decision would be TP(FP) at this cut off
make_TP = udf(TP,  ArrayType(IntegerType()))
make_FP = udf(FP,  ArrayType(IntegerType()))

# Generate these arrays in the dataframe returned by prediction
prediction = prediction.withColumns({'TP':make_TP(prediction.prob_pos, prediction.label), 'FP':make_FP(prediction.prob_pos, prediction.label)})

# Produce a pair-wise sum of these arrays over the entire dataframe, calculate total true positive along the way   
num_cols = len(CutOffs)
TP_FP_pd = prediction.agg(array(*[_sum(col("TP")[i]) for i in range(num_cols)]).alias("sumTP"),
                        array(*[_sum(col("FP")[i]) for i in range(num_cols)]).alias("sumFP"),
                        _sum(col("label")).alias("Positives")
                        )\
                        .toPandas()

# Convert the result into the pd df of precisions and recalls for each cu-off
results_pd= pd.DataFrame({'Cutoff':CutOffs, 'TP':TP_FP_pd.iloc[0,0], 'FP':TP_FP_pd.iloc[0,1]})
results_pd['Precision'] = 100*results_pd['TP']/(results_pd['TP'] + results_pd['FP'])
results_pd['Recall']= 100*results_pd['TP']/TP_FP_pd.iloc[0,2]
results_pd.to_csv('../Data/Eng_LR_prec_rec_5Y.csv')
results_pd

# COMMAND ----------

# Create an object model that is heavily biased toward LASSO regularization
Sig_w_dic = {}
for lbl, weight in zip(lbl_lst, list(models[0].coefficients)):
    if weight >0.001:
        Sig_w_dic[lbl] = weight
Sig_w_dic

# COMMAND ----------

