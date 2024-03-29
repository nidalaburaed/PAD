import random
from time import gmtime
import os
import sys
import bz2
import pandas
import argparse
import pickle
#import altair as alt
from flask import Flask, render_template, abort, Response
from datetime import datetime, timedelta
from prometheus_client import CollectorRegistry, generate_latest, REGISTRY, Counter, Gauge, Histogram
from prometheus import Prometheus
from model import *
from ceph import CephConnect as cp
from ast import literal_eval
# Scheduling stuff
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import atexit



app = Flask(__name__)

data_window = int(os.getenv('DATA_WINDOW_SIZE',60)) # Number of days of past data, the model should use to train

url = os.getenv('URL', None)
token = os.getenv('BEARER_TOKEN')

https_proxy = os.getenv('https_proxy', 'http://fihel1d-proxy.emea.nsn-net.net:8080')

# Specific metric to run the model on
metric_name = os.getenv('METRIC_NAME', None)
print("Using Metric {}.".format(metric_name))

# This is where the model dictionary will be stored and retrieved from
data_storage_path = "Data_Frames" + "/" + url[8:] + "/"+ metric_name + "/" + "prophet_model" + ".pkl"


# Chunk size, download the complete data, but in smaller chunks, should be less than or equal to DATA_SIZE_HOUR
chunk_size = str(os.getenv('CHUNK_SIZE','1h'))

# Net data size to scrape from prometheus
data_size_hour = str(os.getenv('DATA_SIZE_HOUR','12h'))

train_schedule = int(os.getenv('TRAINING_REPEAT_HOURS',3))


TRUE_LIST = ["True", "true", "1", "y"]

store_intermediate_data = os.getenv("STORE_INTERMEDIATE_DATA", "False") # Setting this to true will store intermediate dataframes to ceph


if str(os.getenv('GET_OLDER_DATA',"False")) in TRUE_LIST:
    print("Collecting previously stored data from {}".format(data_storage_path))
    data_dict = cp().get_latest_df_dict(data_storage_path) # Need error handling inside this function, in case the storage path does not exist
    pass
else:
    data_dict = {}


config_list = []                                   #"{'__name__': 'elasticsearch_indices_segments_count', 'cluster': 'es-cluster-avaespoo2', 'es_client_node': 'true', 'es_data_node': 'true', 'es_ingest_node': 'true', 'es_master_node': 'true', 'host': '192.168.7.126', 'instance': 'elasticsearchexporter-monitoring.marathon.mesos:9108', 'job': 'elasticsearch', 'name': '03fbd491de77'}
fixed_label_config = str(os.getenv("LABEL_CONFIG", "{'__name__': 'elasticsearch_indices_segment_count_total', 'index': 'logstash-2019.12.05', 'instance': 'elasticsearchexporter-monitoring.marathon.mesos:9108', 'job': 'elasticsearch'}")) # by default it will train for all label configurations. WARNING: Tthat might take a lot of time depending on your metrics and cpu
if fixed_label_config  != "None":
    config_list = fixed_label_config.split(";") # Separate multiple label configurations using a ';' (semi-colon)
    fixed_label_config_dict = literal_eval(config_list[0]) # # TODO: Add more error handling here


predictions_dict_prophet = {}
predictions_dict_fourier = {}
current_metric_metadata = ""
current_metric_metadata_dict = {}

iteration = 0
def job(current_time):
    # TODO: Replace this function with model training function and set up the correct IntervalTrigger time
    global data_dict, predictions_dict_prophet, predictions_dict_fourier, current_metric_metadata, current_metric_metadata_dict, data_window, url, token, chunk_size, data_size_hour, TRUE_LIST, store_intermediate_data
    global data, config_list, iteration
    #sample_data_path = "C:/Users/naburaed/IdeaProjects/metric-based-anomaly-detection-prometheus/sample_data.json"
    iteration += 1
    print(iteration)
    start_time = time.time()
    prom = Prometheus(url=url, token=token, data_chunk=chunk_size, stored_data=data_size_hour)
    metric = prom.get_metric(metric_name)

    # Convert data to json
    metric = json.loads(metric)
    data_dict = {}
    # Metric Json is converted to a shaped dataframe
    data_dict = get_df_from_json(metric, data_dict, data_window) # This dictionary contains all the sub-labels as keys and their data as Pandas DataFrames
    #print(data_dict)
    del metric #- , prom

    if str(store_intermediate_data) in TRUE_LIST:
        print("DataFrame stored at: ",cp().store_data(metric_name, pickle.dumps(data_dict), (data_storage_path + str(datetime.now().strftime('%Y%m%d%H%M')))))
        pass


    if fixed_label_config != "None": #If a label config has been specified
        single_label_data_dict = {}

        # split into multiple label configs
        existing_config_list = list(data_dict.keys())
        for config in config_list:
            config_found = False
            for existing_config in existing_config_list:
                if SortedDict(literal_eval(existing_config)) == SortedDict(literal_eval(config)):
                    single_label_data_dict[existing_config] = data_dict[existing_config]
                    config_found = True
                    pass
            if not config_found:
                print("Specified Label Configuration {} was not found".format(config))
                raise KeyError
                pass
            # single_label_data_dict[config] = data_dict[config]
            pass

        # single_label_data_dict[fixed_label_config] = data_dict[fixed_label_config]
        current_metric_metadata = list(single_label_data_dict.keys())[0]
        current_metric_metadata_dict = literal_eval(current_metric_metadata)

        print(data_dict[current_metric_metadata].head(5))
        print(data_dict[current_metric_metadata].tail(5))

        print("Using the default label config")
        predictions_dict_prophet = predict_metrics(single_label_data_dict)
        print(single_label_data_dict)
        predictions_dict_fourier = predict_metrics_fourier(single_label_data_dict)
        pass
    else:
        for x in data_dict:
            print(data_dict[x].head(5))
            print(data_dict[x].tail(5))
            break
            pass
        predictions_dict_prophet = predict_metrics(data_dict)
        predictions_dict_fourier = predict_metrics_fourier(data_dict)

    # TODO: Trigger Data Pruning here
    function_run_time = time.time() - start_time

    print("Total time taken to train was: {} seconds.".format(function_run_time))
    pass

job(datetime.now())

# Schedular schedules a background job that needs to be run regularly
scheduler = BackgroundScheduler()
scheduler.start()
scheduler.add_job(
    func=lambda: job(datetime.now()),
    trigger=IntervalTrigger(hours=train_schedule),
    id='training_job',
    name='Train Prophet model every day regularly',
    replace_existing=True)

# Shut down the scheduler when exiting the app
atexit.register(lambda: scheduler.shutdown())



# Initialize Multiple gauge metrics for the predicted values
print("current_metric_metadata_dict: ", current_metric_metadata_dict)
predicted_metric_name = "predicted_" + metric_name
PREDICTED_VALUES_PROPHET = Gauge(predicted_metric_name + '_prophet', 'Forecasted value from Prophet model', ['value_type'])
PREDICTED_VALUES_PROPHET_UPPER = Gauge(predicted_metric_name + '_prophet_yhat_upper', 'Forecasted value upper bound from Prophet model', ['value_type'])
PREDICTED_VALUES_PROPHET_LOWER = Gauge(predicted_metric_name + '_prophet_yhat_lower', 'Forecasted value lower bound from Prophet model', ['value_type'])

PREDICTED_VALUES_FOURIER = Gauge(predicted_metric_name + '_fourier', 'Forecasted value from Fourier Transform model', ['value_type'])
PREDICTED_VALUES_FOURIER_UPPER = Gauge(predicted_metric_name + '_fourier_yhat_upper', 'Forecasted value upper bound from Fourier Transform model', ['value_type'])
PREDICTED_VALUES_FOURIER_LOWER = Gauge(predicted_metric_name + '_fourier_yhat_lower', 'Forecasted value lower bound from Fourier Transform model',  ['value_type'])

PREDICTED_ANOMALY_PROPHET = Gauge(predicted_metric_name + '_prophet_anomaly', 'Detected Anomaly using the Prophet model', ['value_type'])

PREDICTED_ANOMALY_FOURIER = Gauge(predicted_metric_name + '_fourier_anomaly', 'Detected Anomaly using the Fourier model', ['value_type'])

# Standard Flask route stuff.

@app.route('/plots')
def graphs():

    graph_prophet_url = graph_forecast_prophet()
    graph_prophet_components_url = graph_forecast_prophet_components()

    return render_template('graphs.html',
                           prophet=graph_prophet_url,
                           prophet_components=graph_prophet_components_url)


live_data_dict = {}

@app.route('/metrics')
def metrics():
    global predictions_dict_prophet, predictions_dict_fourier, current_metric_metadata, current_metric_metadata_dict, metric_name, url, token, live_data_dict


    for metadata in predictions_dict_prophet:

        #Find the index matching with the current timestamp
        index_prophet = predictions_dict_prophet[metadata].index.get_loc(datetime.utcnow(), method='nearest')
        index_fourier = predictions_dict_fourier[metadata].index.get_loc(datetime.utcnow(), method='nearest')
        current_metric_metadata = metadata

        print("The current time is: ",datetime.utcnow())
        print("The matching index for Prophet model found was: \n", predictions_dict_prophet[metadata].iloc[[index_prophet]])
        print("The matching index for Fourier Transform found was: \n", predictions_dict_fourier[metadata].iloc[[index_fourier]])

        current_metric_metadata_dict = literal_eval(metadata)

        temp_current_metric_metadata_dict = current_metric_metadata_dict.copy()


        # delete the "__name__" key from the dictionary as we don't need it in labels (it is a non-permitted label) when serving the metrics
        del temp_current_metric_metadata_dict["__name__"]

        # TODO: the following function does not have good error handling or retry code in case of get request failure, need to fix that
        # Get the current metric value which will be compared with the predicted value to detect an anomaly
        metric = (Prometheus(url=url, token=token).get_current_metric_value(metric_name, temp_current_metric_metadata_dict))

        # print("metric collected.")

        # Convert data to json
        metric = json.loads(metric)

        #sample_data_path = "C:/Users/naburaed/IdeaProjects/metric-based-anomaly-detection-prometheus/sample_data.json"
        # Load sample data from file
        #with open(sample_data_path, 'r') as f:
        #    metric = json.loads(f.read())
        #print("metric collected.")

        # Convert the json to a dictionary of pandas dataframes
        live_data_dict = get_df_from_single_value_json(metric, live_data_dict)

        # Trim the live data dataframe to only 5 most recent values
        live_data_dict[metadata] = live_data_dict[metadata][-5:]
        #print(live_data_dict)

        # Update the metric values for prophet model
        PREDICTED_VALUES_PROPHET.labels(value_type='yhat').set(predictions_dict_prophet[metadata]['yhat'][index_prophet])
        PREDICTED_VALUES_PROPHET_UPPER.labels(value_type='yhat_upper').set(predictions_dict_prophet[metadata]['yhat_upper'][index_prophet])
        PREDICTED_VALUES_PROPHET_LOWER.labels(value_type='yhat_lower').set(predictions_dict_prophet[metadata]['yhat_lower'][index_prophet])

        # Update the metric values for fourier transform model
        PREDICTED_VALUES_FOURIER.labels(value_type='yhat').set(predictions_dict_fourier[metadata]['yhat'][index_fourier])
        PREDICTED_VALUES_FOURIER_UPPER.labels(value_type='yhat_upper').set(predictions_dict_fourier[metadata]['yhat_upper'][index_fourier])
        PREDICTED_VALUES_FOURIER_LOWER.labels(value_type='yhat_lower').set(predictions_dict_fourier[metadata]['yhat_lower'][index_fourier])

        if len(live_data_dict[metadata]) >= 5:
            pass
            # Update the metric values for detected anomalies 1 in case of anomaly, 0 if not
            if (detect_anomalies(predictions_dict_fourier[metadata][len(predictions_dict_fourier[metadata])-(len(live_data_dict[metadata])):],live_data_dict[metadata])):
                PREDICTED_ANOMALY_FOURIER.labels(value_type='Anomaly').set(1)
                print("Fourier Anomaly")
            else:
                PREDICTED_ANOMALY_FOURIER.labels(value_type='Normal').set(0)
                print("Fourier Normal")

            if (detect_anomalies(predictions_dict_prophet[metadata][len(predictions_dict_prophet[metadata])-(len(live_data_dict[metadata])):],live_data_dict[metadata])):
                PREDICTED_ANOMALY_PROPHET.labels(value_type='Anomaly').set(1)
                print("Prophet Anomaly")
            else:
                PREDICTED_ANOMALY_PROPHET.labels(value_type='Normal').set(0)
                print("Prophet Normal")
        pass

    return Response(generate_latest(REGISTRY).decode("utf-8"), content_type='text; charset=utf-8')

if __name__ == "__main__":
    # Running the flask web server
    app.run(host='0.0.0.0', port=8080)
    pass
