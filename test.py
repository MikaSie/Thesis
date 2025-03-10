import utils.models, utils.tools
import os
import torch
import warnings
import math
import argparse
import logging
import json
import numpy as np
import torch.nn as nn
import wandb
from training import write_predicted_summaries_to_file
from huggingface_hub import whoami
from langchain.text_splitter import TokenTextSplitter
from transformers import DataCollatorForSeq2Seq, Seq2SeqTrainer, Seq2SeqTrainingArguments, EarlyStoppingCallback, AutoTokenizer, AutoModelForSeq2SeqLM
from datasets import load_dataset
from datetime import date
from peft import LoraConfig, get_peft_model, AutoPeftModelForCausalLM
from utils.tools import *

warnings.filterwarnings('ignore', category=FutureWarning, message='^The default value of `n_init` will change from 10 to \'auto\' in 1.4')


def write_actual_summaries_and_references_to_file():
    """
    Writes the actual summaries from the 'eur-lex-sum' dataset to a file named 'actual_summaries.txt'.
    ONLY NEEDS TO BE RUN ONCE TO WRITE THE ACTUAL SUMMARIES TO A FILE.

    This function loads the 'eur-lex-sum' dataset, opens a file in write mode, and writes the actual summaries
    from the 'test' subset of the dataset to the file. Each summary is preceded by a header indicating its index.

    Parameters:
        None

    Returns:
        None
    """
    dataset = load_dataset("dennlinger/eur-lex-sum", 'english', trust_remote_code=True)
    
    path = os.path.join('results', 'actual_summaries.txt')

    with open(path, 'w') as f:
        for i in range(len(dataset['test'])):
            f.write(f"Summary {i}:\n")
            f.write(dataset['test']['summary'][i] + '\n\n\n\n')
    f.close()

    path = os.path.join('results', 'references.txt')

    with open(path, 'w') as f:
        for i in range(len(dataset['test'])):
            f.write(f"Reference {i}:\n")
            f.write(dataset['test']['reference'][i] + '\n\n\n\n')

    print("References and summaries and  written to file.")


def set_device(abstractive_model, args):
    """
    Sets the device for the abstractive model based on the availability of CUDA.

    Parameters:
    - abstractive_model: The abstractive model to be set on the device.
    - args: Command-line arguments containing the device configuration.

    Returns:
    - num_gpu: The number of GPUs available for training.
    """
    if torch.cuda.is_available():
        num_gpu = torch.cuda.device_count()
        device = torch.device('cuda')
        abstractive_model.to(device)
        if args.verbose:
            print(f"Using abstractive model on {num_gpu} devices")

    return num_gpu  


def calculate_word_length_summary(example): 
    """
    Calculates the word length of the summary in the given example.

    Args:
        example (dict): A dictionary containing the example data.

    Returns:
        dict: A dictionary with the word length of the summary.

    """
    return {'word_length': len(example['summary'].split())}


def remove_outliers_from_dataset(dataset):
    """
    Removes outliers from the dataset based on word length of the summaries.

    Args:
        dataset (Dataset): The dataset to remove outliers from.

    Returns:
        dataset (Dataset): The dataset with outliers removed.
    """

    #dataset = load_dataset("dennlinger/eur-lex-sum", 'english', trust_remote_code=True)
    averages = []
    for data in dataset:
        for example in dataset[data]:
            averages.append(example['word_length'])

    mean_token_length = np.mean(averages)
    std = np.std(averages)

    print(f"Before filter: {len(dataset['train'])+len(dataset['validation'])+len(dataset['test'])}. Train: {len(dataset['train'])} Validation: {len(dataset['validation'])} Test: {len(dataset['test'])}")
    dataset = dataset.filter(lambda example: example['word_length'] < (mean_token_length + 2 * std))
    print(f"After filter: {len(dataset['train'])+len(dataset['validation'])+len(dataset['test'])}. Train: {len(dataset['train'])} Validation: {len(dataset['validation'])} Test: {len(dataset['test'])}")

    return dataset


def add_prefix(batch):
    """
    Add the prefix 'summarize: ' to each reference in the batch.

    Args:
        batch (dict): A dictionary containing the batch data.

    Returns:
        dict: The updated batch with the prefix added to each reference.
    """

    batch['reference'] = ['summarize: ' + ref for ref in batch['reference']]

    return batch


def calculate_token_length(example):
    """
    Calculates the token length of a given example.

    Parameters:
    example (dict): The example containing the reference text.

    Returns:
    dict: A dictionary with the token length of the reference text.
    """
    return {'token_length': extractive_tokenizer(example['reference'], return_tensors='pt')['input_ids'].shape[1]}


def get_dependent_compression_ratio(example):
    """
    Calculates the dependent compression ratio for a given example.

    Parameters:
    example (dict): A dictionary containing the example information, including the token length.

    Returns:
    dict: A dictionary containing the dependent compression ratio.
    """

    dependent_ratio = (context_length_abstractive_model / example['token_length'])

    # If the dependent ratio is larger than 1, set it to 1 because we cannot compress more than the original text
    if dependent_ratio > 1:
        dependent_ratio = 1

    return {'dependent_compression_ratio': dependent_ratio}


def calculate_extractive_steps(example):
    """
    Calculates the amount of extractive steps based on the given example.

    Args:
        example (dict): A dictionary containing the example information.

    Returns:
        dict: The updated example dictionary with the amount of extractive steps calculated.
    """

    outcome = (math.log10(context_length_abstractive_model / example["token_length"])) / (math.log10(args.compression_ratio / 10))
    
    # Check here if an outcome is smaller than 0, it should be set to 0. This way we can avoid negative values.
    # Otherwise, if value is -1.4 it will be set to -1. This isn't possible.
    if outcome < 0:
        example["amount_of_extractive_steps"] = 0
    else:
        example["amount_of_extractive_steps"] = math.ceil(outcome)
    
    return example


def get_summarized_chunks(example):
    """
    Generate summarized chunks of text based on the given example.

    Args:
        example (dict): A dictionary containing the example information, including the reference text,
                        the mode of summarization, the amount of extractive steps, and the compression ratio.

    Returns:
        dict: A dictionary containing the concatenated summary of the text chunks.

    """

    text = example["reference"]
    # In case of fixed compression ratio
    if args.mode == 'fixed':
        for _ in range(example["amount_of_extractive_steps"]):
            chunks = text_splitter.split_text(text)
            summaries = []
            for chunk in chunks:
                summary = extractive_model(chunk, ratio= args.compression_ratio / 10)
                summaries.append(summary)

            text = " ".join(summaries)

    elif args.mode == 'dependent':
        chunks = text_splitter.split_text(text)
        summaries = []
        for chunk in chunks:
            summary = extractive_model(chunk, ratio = example["dependent_compression_ratio"])
            summaries.append(summary)
        text = " ".join(summaries)


    elif args.mode == "hybrid":
        ratio = args.compression_ratio / 10
        for i in range(example["amount_of_extractive_steps"]):

            if i == example["amount_of_extractive_steps"] - 1:
                ratio = utils.tools.calculate_hybrid_final_step_ratio(text, context_length_abstractive_model, extractive_tokenizer)
                
            # If the ratio is larger than 1, skip iteration as summarization is not needed!
            if ratio > 1:
                continue
            chunks = text_splitter.split_text(text)
            summaries = []
            for chunk in chunks:
                summary = extractive_model(chunk, ratio = ratio)
                summaries.append(summary)

            text = " ".join(summaries)

    return {'concatenated_summary': text}


def get_summarized_chunks_batch_version(batch):
    """
    Generate summarized chunks from a batch of texts based on the specified mode.

    Args:
        batch (dict): A dictionary containing the batch data, including the reference texts and other parameters.

    Returns:
        dict: A dictionary containing the concatenated summaries of the chunks.

    """

    texts = batch["reference"]
    concatenated_summaries = []

    i = 0 
    for text in texts:
        # In case of fixed compression ratio
        if args.mode == 'fixed':
            for _ in range(batch["amount_of_extractive_steps"][i]):
                chunks = text_splitter.split_text(text)
                print(len(chunks))
                chunk_summaries = []
                for chunk in chunks:
                    summary = extractive_model(chunk, ratio=args.compression_ratio / 10)
                    chunk_summaries.append(summary)
                text = " ".join(chunk_summaries)

        elif args.mode == 'dependent':
            chunks = text_splitter.split_text(text)
            chunk_summaries = []
            for chunk in chunks:
                summary = extractive_model(chunk, ratio=batch["dependent_compression_ratio"][i])
                chunk_summaries.append(summary)
            text = " ".join(chunk_summaries)


        elif args.mode == "hybrid":

            ratio = args.compression_ratio / 10
            for x in range(batch["amount_of_extractive_steps"][i]):

                if x == batch["amount_of_extractive_steps"][i] - 1:
                    ratio = utils.tools.calculate_hybrid_final_step_ratio(text, context_length_abstractive_model, extractive_tokenizer)
                # If the ratio is larger than 1, skip iteration as summarization is not needed!
                
                if ratio > 1:
                    continue
                chunks = text_splitter.split_text(text)
                chunk_summaries = []
                for chunk in chunks:
                    summary = extractive_model(chunk, ratio=ratio)
                    chunk_summaries.append(summary)
                text = " ".join(chunk_summaries)

        i+=1

        concatenated_summaries.append(text)

    return {'concatenated_summary': concatenated_summaries}


def get_feature(batch):
    """
    Get the feature encodings for a given batch.

    Args:
        batch (dict): A dictionary containing the batch data.

    Returns:
        dict: The feature encodings, including input_ids, attention_mask, and labels.
    """
    if args.no_extraction:
        encodings = abstractive_tokenizer(batch['reference'], text_target=batch['summary'],
                        max_length=context_length_abstractive_model, truncation=True)
    else:
        encodings = abstractive_tokenizer(batch['concatenated_summary'], text_target=batch['summary'],
                        max_length=context_length_abstractive_model)

    encodings = {'input_ids': encodings['input_ids'],
                 'attention_mask': encodings['attention_mask'],
                 'labels': encodings['labels']}

    return encodings


def compute_rouge_during_training(pred):

    labels_ids = pred.label_ids
    pred_ids = pred.predictions

    labels_ids[labels_ids == -100] = abstractive_tokenizer.pad_token_id
    label_str = abstractive_tokenizer.batch_decode(labels_ids, skip_special_tokens=True)
    
    pred_ids[pred_ids == -100] = abstractive_tokenizer.pad_token_id
    pred_str = abstractive_tokenizer.batch_decode(pred_ids, skip_special_tokens=True)

    rouge_output = rouge_evaluation_metric.compute(predictions = pred_str, references = label_str, rouge_types = ["rouge1", "rouge2", "rougeL"])

    return {**rouge_output}


def preprocess_logits_for_metrics(logits, labels):

    pred_ids = torch.argmax(logits[0], dim=-1)

    return pred_ids, labels


def print_trainable_parameters(model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    print(
        f"trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param:.2f}"
    )


if __name__ == "__main__":
    
    parser = argparse.ArgumentParser(description = "Train an abstractive model on the EUR-Lex dataset which is pre-processed with an extractive model at a certain extractive compression ratio.")

    parser.add_argument('extractive_model', type= str, 
                        help= "The extractive model to be used for pre-processing the dataset.")
    parser.add_argument('compression_ratio', type= int, default= 4, choices= range(1, 10),
                        help= "The compression ratio to be used for the extractive model. Is in the form of an integer where 5 is 0.5, 9 is 0.9, etc.")
    parser.add_argument('abstractive_model', type= str,
                        help= "The abstractive model to be used for fine-tuning.")
    
    #Optional arguments
    parser.add_argument('-t', '--testing_only', action= "store_true", default= False,
                        help= "Train the abstractive model. If not set, the model will not be trained and only the evaluation metrics will be calculated.")
    parser.add_argument('-m', '--mode', choices= ['fixed', 'dependent', 'hybrid'], type= str, default= 'dependent',
                        help= "The ratio mode to use for the extractive summarization stage.")
    parser.add_argument('-nte', '--num_train_epochs', type= int, default= 40, metavar= "",
                        help= "The amount of epochs to train the abstractive model for.")
    parser.add_argument('-b', '--batch_size', type= int, default= 16, metavar= "",
                        help= "The batch size to train the abstractive model with.")
    parser.add_argument('-gas', '--gradient_accumulation_steps', type= int, default= 1, metavar= "",
                        help= "The amount of gradient accumulation steps to train the abstractive model with.")
    parser.add_argument('-gc', '--gradient_checkpointing', action= "store_true", default= False,
                        help= "Use gradient checkpointing to train the abstractive model.")
    parser.add_argument('-fp16', '--fp16', action= "store_true", default= False,
                        help= "Use mixed precision training to train the abstractive model.")
    parser.add_argument('-bf16', '--bf16', action= "store_true", default= False,
                        help= "Use bfloat16 precision training to train the abstractive model.")
    parser.add_argument('-w', '--warmup_ratio', type= float, default= 0.1, metavar= "",
                        help= "The warmup ratio to train the abstractive model for.")
    parser.add_argument('-v', '--verbose', action= "store_false", default= True,
                        help= "Turn verbosity on or off.")
    parser.add_argument('-wd', '--weight_decay', type= float, default= 0.01, metavar= "",
                        help= "The weight decay to train the abstractive model with.")
    parser.add_argument('-eas', '--eval_accumulation_steps', type= int, default= 1, metavar= "",
                        help= "The amount of accumulation steps to use during evaluation.")
    parser.add_argument('-lbm', '--load_best_model_at_end', action= "store_false", default= True,
                        help= "Load the best model at the end of training.")
    parser.add_argument('-es', '--early_stopping_patience', type= int, default= 5, metavar= "",
                        help= "The amount of patience to use for early stopping.")
    parser.add_argument('-mfm', '--metric_for_best_model', type= str, default= "eval_loss", metavar= "",
                        help= "The metric to use for selection of the best model.")
    parser.add_argument('-ne', '--no_extraction', action= "store_true", default= False,
                        help= "Finetune a model on the whole dataset without any extractive steps.")                
    parser.add_argument('-wr', '--write_actual_summaries_and_references_to_file', action= "store_true", default= False,
                        help= "Write the actual summaries to a txt file for reference.")
    parser.add_argument('-po', '--preprocessing_only', action= "store_true", default= False,
                        help= "Only preprocess the dataset and exit the program.")
    parser.add_argument('-lsb', '--Llama_super_batch', action= "store_true", default= False,
                        help= "Use super batch testing for Llama3 model.") 

    args = parser.parse_args()  

    extractive_model, extractive_tokenizer = utils.models.select_extractive_model(args.extractive_model)
    
    evaluation_results_filepath = os.path.join('results', 'evaluation_results.json')

    if args.testing_only:
        model_id, model_version, previous_results = utils.tools.get_id_and_version_and_prev_results(evaluation_results_filepath, args)

    else:
        model_id, model_version, previous_results = utils.tools.get_id_and_version_and_prev_results(evaluation_results_filepath, args)
        #abstractive_model, abstractive_tokenizer = utils.models.select_abstractive_model(args.abstractive_model)
        #print(f"Loaded a {args.abstractive_model} model with new model id {model_id} to be used for training and testing.")

    print(f"Model_id to be evaluated: {model_id}")

    if args.write_actual_summaries_and_references_to_file:
        write_actual_summaries_and_references_to_file()

    #num_gpu = set_device(abstractive_model, args)
    num_gpu = torch.cuda.device_count()

    dataset = load_dataset("dennlinger/eur-lex-sum", 'english', trust_remote_code = True)  
    dataset = dataset.map(calculate_word_length_summary)
    dataset = remove_outliers_from_dataset(dataset)


    if args.verbose:
        print(f"Length of the dataset: Train: {len(dataset['train'])} Validation: {len(dataset['validation'])} Test: {len(dataset['test'])}")

    # This extra check is implemented to ensure that the same dataset is used every time! Sometimes, this went wrong.
    if (len(dataset['train']) == 1129 and len(dataset['validation']) == 187 and len(dataset['test']) == 188):
        print("Dataset is being adjusted to the correct size.....")
        dataset = dataset.map(calculate_word_length_summary)
        dataset = remove_outliers_from_dataset(dataset)

    # Additional pre-processing is done here because the dataset is loaded from disk and the columns are not loaded with it. This way it is easier to remove the columns we don't need.    
    label_str = dataset["test"]["summary"]
    reference_str = dataset["test"]["reference"]

    # Remove the columns from all datasets
    columns_to_keep = ["input_ids", "attention_mask", "labels"]
    all_datasets = ["train", "validation", "test"]
    for dataset_name in all_datasets:
        all_columns = dataset[dataset_name].column_names
        columns_to_remove = [col for col in all_columns if col not in columns_to_keep]
        dataset[dataset_name] = dataset[dataset_name].remove_columns(columns_to_remove)
    

    if args.verbose:
        print("Dataset preprocessed and ready for the next step.")

    # Models are deleted to save space for training. For RoBERTa, around 13GB is freed up!
    del extractive_model, extractive_tokenizer

    #del abstractive_model, abstractive_tokenizer
        
    
    if args.Llama_super_batch:
        start_index_list = [0,30,60,90,120,150]
        pred_str = []
        for start_index in start_index_list:
            file_path = os.path.join('results', 'text_outputs', f"{model_id}_predictions_start_index{start_index}.txt")
            small_index_pred_list = utils.tools.read_created_summaries(file_path)
            pred_str.extend(small_index_pred_list)

        file_path = os.path.join('results', 'text_outputs', f"{model_id}_predictions.txt")
        # Only write to file if it doesn't exist, otherwise,we just use the predictions from the files found.
        if not os.path.exists(file_path):
            write_predicted_summaries_to_file(file_path, pred_str, start_index=0)

    # Load the text file content
    else:
        file_path = os.path.join('results', 'text_outputs', f"{model_id}_predictions.txt")
        pred_str = utils.tools.read_created_summaries(file_path)


    rouge_scores = calculate_rouge_score(predictions = pred_str, references = label_str)
    print("Calculated ROUGE scores")

    bert_score = calculate_bert_score(predictions = pred_str, references = label_str, batch_size = args.batch_size)
    print("Calculated BERT scores")

    bart_score =  calculate_bart_score(predictions = pred_str, references = label_str, batch_size = args.batch_size)
    print("Calculated BART scores")

    blanc_score = calculate_blanc_score(predictions = pred_str, references = reference_str, batch_size = args.batch_size)
    print("Calculated BLANC scores")
    new_result = next((item for item in previous_results if item["Model_ID"] == model_id), None)

    new_result["Evaluation_metrics"] = {
                "ROUGE-1": rouge_scores['rouge1'],
                "ROUGE-2": rouge_scores['rouge2'],
                "ROUGE-L": rouge_scores['rougeL'],
                "BERTScore": bert_score,
                "BARTScore": bart_score,
                "BLANC": blanc_score
    }

        
         # Convert to JSON and write to a file
    with open(evaluation_results_filepath, 'w') as f:
        json.dump(previous_results, f, indent=4)
    f.close()

    model_card = utils.tools.create_model_card(new_result)

    # Only MikaSie can push to the hub
    user = whoami()['name']
    model_card.push_to_hub(repo_id = f"{user}/{model_id}", repo_type= "model")
        

    if args.verbose:
        print(f"Results saved to {evaluation_results_filepath} and model card pushed to the hub.")