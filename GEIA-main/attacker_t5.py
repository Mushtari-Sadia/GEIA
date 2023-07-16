import os
os.environ["CUDA_VISIBLE_DEVICES"] = "0"

import torch
import torch.nn as nn
import torch.nn.functional as F
from scipy.spatial.distance import cosine

import json
import numpy as np
import pandas as pd
import argparse
import sys

from transformers import AutoModel, AutoTokenizer
from transformers import AutoModelForCausalLM,GPT2Config,GPT2LMHeadModel
from transformers import AdamW,get_linear_schedule_with_warmup
from transformers import T5Tokenizer, T5ForConditionalGeneration,T5Config

from torch.utils.data import DataLoader, Dataset
from attacker_models import SequenceCrossEntropyLoss
from sentence_transformers import SentenceTransformer
from simcse_persona import get_persona_dict
from attacker_evaluation_gpt import eval_on_batch
from datasets import load_dataset
from data_process import get_sent_list

Folder_path = 'models/'
class linear_projection(nn.Module):
    def __init__(self, in_num, out_num=1024):
        super(linear_projection, self).__init__()
        self.fc1 = nn.Linear(in_num, out_num)

    def forward(self, x, use_final_hidden_only = True):
        # x should be of shape (?,in_num) according to gpt2 output
        out_shape = x.size()[-1]
        assert(x.size()[1] == out_shape)
        out = self.fc1(x)


        return out


class personachat(Dataset):
    def __init__(self, data):
        self.data = data


    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, index):
        
        text = self.data[index]

        return  text
        
    def collate(self, unpacked_data):
        return unpacked_data


def init_opt():
    #opt-350m
    T5Config
    config = T5Config.from_pretrained('google/t5-large-lm-adapt')
    model = T5ForConditionalGeneration(config)
    #model = T5ForConditionalGeneration.from_pretrained("google/t5-large-lm-adapt")
    tokenizer = T5Tokenizer.from_pretrained("google/t5-large-lm-adapt")
    #tokenizer = AutoTokenizer.from_pretrained('facebook/opt-1.3b',use_fast=False)
    #model = GPT2LMHeadModel(config)
    return model, tokenizer

def process_data(data,batch_size,device,config,need_proj=True):
    #model = SentenceTransformer('all-roberta-large-v1',device=device)   # dim 1024
    embed_model_name = config['embed_model']
    model = SentenceTransformer(config['embed_model_path'],device=device)   # dim 768
    dataset = personachat(data)
    dataloader = DataLoader(dataset=dataset, 
                              shuffle=True, 
                              batch_size=batch_size, 
                              collate_fn=dataset.collate)

    print('load data done')

    ### extra projection
    if need_proj:
        projection = linear_projection(in_num=768).to(device)
    ### for attackers
    #model_attacker = AutoModelForCausalLM.from_pretrained(config['model_dir'])
    #tokenizer_attacker = AutoTokenizer.from_pretrained(config['model_dir'])
    model_attacker, tokenizer_attacker = init_opt()
    criterion = SequenceCrossEntropyLoss()
    model_attacker.to(device)
    param_optimizer = list(model_attacker.named_parameters())
    no_decay = ['bias', 'ln', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
    {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
    {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}

    ]
    num_gradients_accumulation = 1
    num_epochs = config['num_epochs']
    batch_size = config['batch_size']
    num_train_optimization_steps  = len(dataloader) * num_epochs // num_gradients_accumulation
    optimizer = AdamW(optimizer_grouped_parameters, 
                  lr=3e-4,
                  eps=1e-06) #Typically, 1e-4 and 3e-4 work well for most problems (classification, summarization, translation, question answering, question generation)
    if need_proj:
        optimizer.add_param_group({'params': projection.parameters()})
    scheduler = get_linear_schedule_with_warmup(optimizer, 
                                            num_warmup_steps=100, 
                                            num_training_steps = num_train_optimization_steps)
    
    ### process to obtain the embeddings
    for i in range(num_epochs):
        for idx,batch_text in enumerate(dataloader):
            with torch.no_grad():           

                #sys.exit(-1)          
                embeddings = model.encode(batch_text,convert_to_tensor = True)
                print(f'Embedding dim: {embeddings.size()}')

            ### attacker part, needs training
            if need_proj:
               embeddings = projection(embeddings)

            record_loss, perplexity = train_on_batch(batch_X=embeddings,batch_D=batch_text,model=model_attacker,tokenizer=tokenizer_attacker,criterion=criterion,device=device,train=True)
            optimizer.step()
            scheduler.step()
            # make sure no grad for GPT optimizer
            optimizer.zero_grad()
            print(f'{embed_model_name}: Training: epoch {i} batch {idx} with loss: {record_loss} and PPL {perplexity} with size {embeddings.size()}')
            #sys.exit(-1)
        if need_proj:
            proj_path = Folder_path + 'projection_opt_' + config['dataset'] + '_' + config['embed_model']
            torch.save(projection.state_dict(), proj_path)
        save_path = Folder_path + 'attacker_opt_' + config['dataset'] + '_' + config['embed_model']
        model_attacker.save_pretrained(save_path)



def process_data_simcse(data,batch_size,device,config,need_proj=False):
    embed_model_name = config['embed_model']
    tokenizer = AutoTokenizer.from_pretrained(config['embed_model_path'])  # dim 1024
    model = AutoModel.from_pretrained(config['embed_model_path']).to(device)
    dataset = personachat(data)
    dataloader = DataLoader(dataset=dataset, 
                              shuffle=True, 
                              batch_size=batch_size, 
                              collate_fn=dataset.collate)

    print('load data done')

    ### extra projection
    if need_proj:
        projection = linear_projection(in_num=768).to(device)

    ### for attackers
    #model_attacker = AutoModelForCausalLM.from_pretrained(config['model_dir'])
    #tokenizer_attacker = AutoTokenizer.from_pretrained(config['model_dir'])
    model_attacker, tokenizer_attacker = init_opt()
    criterion = SequenceCrossEntropyLoss()
    model_attacker.to(device)
    param_optimizer = list(model_attacker.named_parameters())
    no_decay = ['bias', 'ln', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
    {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
    {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}

    ]
    num_gradients_accumulation = 1
    num_epochs = config['num_epochs']
    batch_size = config['batch_size']
    num_train_optimization_steps  = len(dataloader) * num_epochs // num_gradients_accumulation
    optimizer = AdamW(optimizer_grouped_parameters, 
                  lr=3e-4,
                  eps=1e-06)
    if need_proj:
        optimizer.add_param_group({'params': projection.parameters()})
    scheduler = get_linear_schedule_with_warmup(optimizer, 
                                            num_warmup_steps=100, 
                                            num_training_steps = num_train_optimization_steps)
    
    ### process to obtain the embeddings
    for i in range(num_epochs):
        for idx,batch_text in enumerate(dataloader):
            with torch.no_grad():           
                inputs = tokenizer(batch_text, padding=True, truncation=True, return_tensors="pt").to(device)
                embeddings = model(**inputs, output_hidden_states=True, return_dict=True).pooler_output     
                print(embeddings.size())


            ### attacker part, needs training
            if need_proj:
               embeddings = projection(embeddings)

            record_loss, perplexity = train_on_batch(batch_X=embeddings,batch_D=batch_text,model=model_attacker,tokenizer=tokenizer_attacker,criterion=criterion,device=device,train=True)
            optimizer.step()
            scheduler.step()
            # make sure no grad for GPT optimizer
            optimizer.zero_grad()
            print(f'{embed_model_name}: Training: epoch {i} batch {idx} with loss: {record_loss} and PPL {perplexity} with size {embeddings.size()}')
            #sys.exit(-1)
        if need_proj:
            proj_path = Folder_path + 'projection_opt_' + config['dataset'] + '_' + config['embed_model']
            torch.save(projection.state_dict(), proj_path)
        save_path = Folder_path + 'attacker_opt_' + config['dataset'] + '_' + config['embed_model']
        model_attacker.save_pretrained(save_path)

### used for testing only
def process_data_test(data,batch_size,device,config,need_proj=False):
    #model = SentenceTransformer('all-roberta-large-v1',device=device)   # dim 1024
    model = SentenceTransformer(config['embed_model_path'],device=device)   # dim 768
    if(config['decode'] == 'beam'):
        save_path = Folder_path + 'attacker_opt_' + config['dataset'] + '_' + config['embed_model']+'_beam'+'.log'
    else:
        save_path = Folder_path + 'attacker_opt_' + config['dataset'] + '_' + config['embed_model']+'.log'
    dataset = personachat(data)
    # no shuffle for testing data
    dataloader = DataLoader(dataset=dataset, 
                              shuffle=False, 
                              batch_size=batch_size, 
                              collate_fn=dataset.collate)

    print('load data done')
    if need_proj:
        proj_path = Folder_path + 'projection_opt_' + config['dataset'] + '_' + config['embed_model']
        projection = linear_projection(in_num=768)
        projection.load_state_dict(torch.load(proj_path))
        projection.to(device)
        print('load projection done')
    else:
        print('no projection loaded')
    # setup on config for sentence generation   AutoModelForCausalLM
    attacker_path = Folder_path + 'attacker_opt_' + config['dataset'] + '_' + config['embed_model']
    config['model'] = T5ForConditionalGeneration.from_pretrained(attacker_path).to(device)
    #config['tokenizer'] = AutoTokenizer.from_pretrained('microsoft/DialoGPT-medium')

    sent_dict = {}
    sent_dict['gt'] = []
    sent_dict['pred'] = []
    with torch.no_grad():  
        for idx,batch_text in enumerate(dataloader):
            embeddings = model.encode(batch_text,convert_to_tensor = True)
            #inputs = tokenizer(batch_text, padding=True, truncation=True, return_tensors="pt").to(device)
            #embeddings = model(**inputs, output_hidden_states=True, return_dict=True).pooler_output  
            if need_proj:
                embeddings = projection(embeddings)
            #embeddings_out = embeddings_proj.detach().cpu()
            #data_dict['text'].extend(batch_text)
            #data_dict['embedding'].extend(embeddings_out)
            sent_list, gt_list = eval_on_batch_t5(batch_X=embeddings,batch_D=batch_text,model=config['model'],tokenizer=config['tokenizer'],device=device,config=config)    
            print(f'testing {idx} batch done with {idx*batch_size} samples')
            sent_dict['pred'].extend(sent_list)
            sent_dict['gt'].extend(gt_list)
        
        with open(save_path, 'w') as f:
            json.dump(sent_dict, f,indent=4)

    return 0


def eval_on_batch_t5(batch_X,batch_D,model,tokenizer,device,config):  
    device = config['device']
    hidden_X_unsqueeze = torch.unsqueeze(batch_X, 1) 
    inputs_embeds = hidden_X_unsqueeze# a 3D tensor, [batch, seq_length, dim] : seq_len = 1
    print(f'inputs_embeds:{inputs_embeds.shape}')
    attention_mask = torch.ones(inputs_embeds.shape[:2], dtype=torch.long).to(device)
    decoder_input_ids = torch.ones((inputs_embeds.shape[0], 1), dtype=torch.long) * model.config.decoder_start_token_id
    decoder_input_ids = decoder_input_ids.to(device)
    output_ids = model.generate(attention_mask=attention_mask, decoder_input_ids=decoder_input_ids, inputs_embeds=inputs_embeds, max_length=50,num_beams=5)  
    gen_sent_list = tokenizer.batch_decode(output_ids,skip_special_tokens=True)
    sent_list = gen_sent_list
    gt_list = batch_D
    i = 2
    print(f'pred:{sent_list[i]}')
    print(f'gt  :{gt_list[i]}')
    return sent_list, gt_list


### used for testing only
def process_data_test_simcse(data,batch_size,device,config,proj_dir=None,need_proj=False):
    tokenizer = AutoTokenizer.from_pretrained(config['embed_model_path'])  # dim 1024
    model = AutoModel.from_pretrained(config['embed_model_path']).to(device)
    #save_path = 'logs/attacker_opt_qnli_simcse_bert_large.log'
    if(config['decode'] == 'beam'):
        print('Using beam search decoding')
        save_path = Folder_path + 'attacker_opt_' + config['dataset'] + '_' + config['embed_model']+'_beam'+'.log'
    else:
        save_path = Folder_path + 'attacker_opt_' + config['dataset'] + '_' + config['embed_model']+'.log'
    dataset = personachat(data)
    # no shuffle for testing data
    dataloader = DataLoader(dataset=dataset, 
                              shuffle=False, 
                              batch_size=batch_size, 
                              collate_fn=dataset.collate)

    print('load data done')
    if need_proj:
        projection = linear_projection(in_num=768)
        projection.load_state_dict(torch.load(proj_dir))
        projection.to(device)
        print('load projection done')
    else:
        print('no projection loaded')
    # setup on config for sentence generation   AutoModelForCausalLM
    attacker_path = Folder_path + 'attacker_opt_' + config['dataset'] + '_' + config['embed_model']
    config['model'] = AutoModelForCausalLM.from_pretrained(attacker_path).to(device)
    config['tokenizer'] = AutoTokenizer.from_pretrained('microsoft/DialoGPT-medium')

    sent_dict = {}
    sent_dict['gt'] = []
    sent_dict['pred'] = []
    with torch.no_grad():  
        for idx,batch_text in enumerate(dataloader):
            inputs = tokenizer(batch_text, padding=True, truncation=True, return_tensors="pt").to(device)
            embeddings = model(**inputs, output_hidden_states=True, return_dict=True).pooler_output  
            if need_proj:
                embeddings = projection(embeddings)
            #sent_list, gt_list = eval_on_batch(batch_X=embeddings,batch_D=batch_text,model=config['model'],tokenizer=config['tokenizer'],device=device,config=config)    
            sent_list, gt_list = eval_on_batch(batch_X=embeddings,batch_D=batch_text,model=config['model'],tokenizer=config['tokenizer'],device=device,config=config) 
            print(f'testing {idx} batch done with {idx*batch_size} samples')
            sent_dict['pred'].extend(sent_list)
            sent_dict['gt'].extend(gt_list)
        with open(save_path, 'w') as f:
            json.dump(sent_dict, f,indent=4)

    return 0


def train_on_batch(batch_X,batch_D,model,tokenizer,criterion,device,train=True):
    #padding_token_id = tokenizer.encode(tokenizer.eos_token)[0]
    tokenizer.pad_token = tokenizer.eos_token
    inputs = tokenizer(batch_D, return_tensors='pt', padding='max_length', truncation=True, max_length=40)
    #dial_tokens = [tokenizer.encode(item) + turn_ending for item in batch_D]
    #print(inputs)
    input_ids = inputs['input_ids'] # tensors of input ids
    labels = input_ids
    # replace padding token id's of the labels by -100 so it's ignored by the loss
    #labels[labels == tokenizer.pad_token_id] = -100
    input_ids = input_ids.to(device)
    labels = labels.to(device)
    #attention_mask = inputs['attention_mask']

    
    batch_size = batch_X.size()[0]
    attention_mask = torch.ones(batch_size,1).to(device)

    #print(input_ids.size())
    # embed the input ids using GPT-2 embedding
    input_emb = model.shared(input_ids)
    # add extra dim to cat together
    batch_X = batch_X.to(device)
    batch_X = torch.unsqueeze(batch_X, 1)
    #inputs_embeds = torch.cat((batch_X_unsqueeze,input_emb),dim=1)   #[batch,max_length+1,emb_dim (1024)]
    assert input_emb.size()[-1] == batch_X.size()[-1]



    #output = model(inputs_embeds=batch_X,attention_mask=attention_mask,labels=labels,return_dict=True,output_hidden_states=True)
    output = model(inputs_embeds=batch_X,labels=labels,return_dict=True)
    # encoder_last_hidden_state = output.encoder_last_hidden_state 
    # last_hidden_state = output.decoder_hidden_states[-1] 
    # print(f'encoder_last_hidden_state:{encoder_last_hidden_state.size()}') #([bs, 1, 1024])
    # print(f'last_hidden_state:{last_hidden_state.size()}') #([bs, 40, 1024])
    loss = output.loss

    record_loss = loss.item()
    perplexity = np.exp(record_loss)
    if train:
        loss.backward()

    return record_loss, perplexity





if __name__ == '__main__':
    '''
    Sentence bert based:
    T5: sentence-t5-large                   dim 768
    mpnet: all-mpnet-base-v1                dim 768
    Roberta: all-roberta-large-v1           dim 1024


    SIMCSE based:
    princeton-nlp/unsup-simcse-roberta-large        dim 1024
    princeton-nlp/sup-simcse-bert-large-uncased     dim 1024
    '''
    model_cards ={}
    model_cards['sent_t5'] = 'sentence-t5-large'
    model_cards['mpnet'] = 'all-mpnet-base-v1'
    model_cards['sent_roberta'] = 'all-roberta-large-v1'
    model_cards['simcse_bert'] = 'princeton-nlp/sup-simcse-bert-large-uncased'
    model_cards['simcse_roberta'] = 'princeton-nlp/sup-simcse-roberta-large'

    parser = argparse.ArgumentParser(description='Training external NN as baselines')
    parser.add_argument('--model_dir', type=str, default='t5', help='Dir of your model')
    parser.add_argument('--num_epochs', type=int, default=10, help='Training epoches.')
    parser.add_argument('--batch_size', type=int, default=32, help='Batch_size #.')
    parser.add_argument('--dataset', type=str, default='personachat', help='Name of dataset: personachat or qnli')
    #parser.add_argument('--dataset', type=str, default='qnli', help='Name of dataset: personachat or qnli')
    #parser.add_argument('--data_type', type=str, default='train', help='train/test')
    parser.add_argument('--data_type', type=str, default='test', help='train/test')
    parser.add_argument('--embed_model', type=str, default='sent_t5', help='Name of embedding model: mpnet/sent_roberta/simcse_bert/simcse_roberta/sent_t5')
    parser.add_argument('--decode', type=str, default='beam', help='Name of decoding methods: beam/sampling')
    #parser.add_argument('--embed_model', type=str, default='simcse_roberta', help='Name of embedding model: mpnet/sent_roberta/simcse_bert/simcse_roberta/sent_t5')
    args = parser.parse_args()
    config = {}
    config['model_dir'] = args.model_dir
    config['num_epochs'] = args.num_epochs
    config['batch_size'] = args.batch_size
    config['dataset'] = args.dataset
    config['data_type'] = args.data_type
    config['embed_model'] = args.embed_model
    config['decode'] = args.decode
    config['embed_model_path'] = model_cards[config['embed_model']]
    config['device'] = torch.device("cuda")
    config['tokenizer'] = T5Tokenizer.from_pretrained("google/t5-large-lm-adapt")
    config['eos_token'] = config['tokenizer'].eos_token

    
    device = torch.device("cuda")
    #device = torch.device("cpu")
    batch_size = config['batch_size']


    sent_list = get_sent_list(config)
    
    ##### for training
    if(config['data_type'] == 'train'):
        process_data(sent_list,batch_size,device,config)
    elif(config['data_type'] == 'test'):
        if('simcse' in config['embed_model'] or 'sent_roberta' in config['embed_model']):
            process_data_test_simcse(sent_list,batch_size,device,config,proj_dir=None,need_proj=False)
        else:
            process_data_test(sent_list,batch_size,device,config,need_proj=True)
