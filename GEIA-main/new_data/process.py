import pickle

#load data with pickle
with open('dataset_automated_medical_transcription-v1.0/nazmulkazi-dataset_automated_medical_transcription-6605327/dataset.pickle', 'rb') as f:
    data = pickle.load(f)
print(data)