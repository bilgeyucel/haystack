from typing import List, Union, Optional, Iterator
import itertools

from transformers import AutoModelForSeq2SeqLM
from transformers import AutoTokenizer

from haystack.errors import HaystackError
from haystack.schema import Document
from haystack.nodes.base import BaseComponent
from haystack.nodes.preprocessor import PreProcessor
from haystack.modeling.utils import initialize_device_settings


class QuestionGenerator(BaseComponent):
    """
    The Question Generator takes only a document as input and outputs questions that it thinks can be
    answered by this document. In our current implementation, input texts are split into chunks of 50 words
    with a 10 word overlap. This is because the default model `valhalla/t5-base-e2e-qg` seems to generate only
    about 3 questions per passage regardless of length. Our approach prioritizes the creation of more questions
    over processing efficiency (T5 is able to digest much more than 50 words at once). The returned questions
    generally come in an order dictated by the order of their answers i.e. early questions in the list generally
    come from earlier in the document.
    """

    outgoing_edges = 1

    def __init__(
        self,
        model_name_or_path="valhalla/t5-base-e2e-qg",
        model_version=None,
        num_beams=4,
        max_length=256,
        no_repeat_ngram_size=3,
        length_penalty=1.5,
        early_stopping=True,
        split_length=50,
        split_overlap=10,
        use_gpu=True,
        prompt="generate questions:",
        num_queries_per_doc=1,
        batch_size: Optional[int] = None,
    ):
        """
        Uses the valhalla/t5-base-e2e-qg model by default. This class supports any question generation model that is
        implemented as a Seq2SeqLM in HuggingFace Transformers. Note that this style of question generation (where the only input
        is a document) is sometimes referred to as end-to-end question generation. Answer-supervised question
        generation is not currently supported.

        :param model_name_or_path: Directory of a saved model or the name of a public model e.g. "valhalla/t5-base-e2e-qg".
                                   See https://huggingface.co/models for full list of available models.
        :param model_version: The version of model to use from the HuggingFace model hub. Can be tag name, branch name, or commit hash.
        :param use_gpu: Whether to use GPU or the CPU. Falls back on CPU if no GPU is available.
        :param batch_size: Number of documents to process at a time.
        """
        super().__init__()
        self.devices, _ = initialize_device_settings(use_cuda=use_gpu, multi_gpu=False)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(model_name_or_path)
        self.model.to(str(self.devices[0]))
        self.tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
        self.num_beams = num_beams
        self.max_length = max_length
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self.length_penalty = length_penalty
        self.early_stopping = early_stopping
        self.split_length = split_length
        self.split_overlap = split_overlap
        self.preprocessor = PreProcessor()
        self.prompt = prompt
        self.num_queries_per_doc = num_queries_per_doc
        self.batch_size = batch_size

    def run(self, documents: List[Document]):  # type: ignore
        generated_questions = []
        for d in documents:
            questions = self.generate(d.content)
            curr_dict = {"document_id": d.id, "document_sample": d.content[:200], "questions": questions}
            generated_questions.append(curr_dict)
        output = {"generated_questions": generated_questions, "documents": documents}
        return output, "output_1"

    def run_batch(self, documents: Union[List[Document], List[List[Document]]], batch_size: Optional[int] = None):  # type: ignore
        generated_questions = []
        if isinstance(documents[0], Document):
            questions = self.generate_batch(
                texts=[d.content for d in documents if isinstance(d, Document)], batch_size=batch_size
            )
            questions_iterator = questions  # type: ignore
            documents_iterator = documents
        else:
            questions = self.generate_batch(
                texts=[[d.content for d in doc_list] for doc_list in documents if isinstance(doc_list, list)],
                batch_size=batch_size,
            )
            questions_iterator = itertools.chain.from_iterable(questions)  # type: ignore
            documents_iterator = itertools.chain.from_iterable(documents)  # type: ignore
        for cur_questions, doc in zip(questions_iterator, documents_iterator):
            if not isinstance(doc, Document):
                raise HaystackError(f"doc was of type {type(doc)}, but expected a Document.")
            curr_dict = {"document_id": doc.id, "document_sample": doc.content[:200], "questions": cur_questions}
            generated_questions.append(curr_dict)
        output = {"generated_questions": generated_questions, "documents": documents}
        return output, "output_1"

    def generate(self, text: str) -> List[str]:
        # Performing splitting because T5 has a max input length
        # Also currently, it seems that it only generates about 3 questions for the beginning section of text
        split_texts_docs = self.preprocessor.split(
            document={"content": text},
            split_by="word",
            split_respect_sentence_boundary=False,
            split_overlap=self.split_overlap,
            split_length=self.split_length,
        )
        split_texts = [
            f"{self.prompt} {text.content}" if self.prompt not in text.content else text.content
            for text in split_texts_docs
        ]
        tokenized = self.tokenizer(split_texts, return_tensors="pt", padding=True)
        input_ids = tokenized["input_ids"].to(self.devices[0])
        # Necessary if padding is enabled so the model won't attend pad tokens
        attention_mask = tokenized["attention_mask"].to(self.devices[0])
        tokens_output = self.model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            num_beams=self.num_beams,
            max_length=self.max_length,
            no_repeat_ngram_size=self.no_repeat_ngram_size,
            length_penalty=self.length_penalty,
            early_stopping=self.early_stopping,
            num_return_sequences=self.num_queries_per_doc,
        )

        string_output = self.tokenizer.batch_decode(tokens_output)
        string_output = [cur_output.replace("<pad>", "").replace("</s>", "") for cur_output in string_output]

        ret = []
        for split in string_output:
            for question in split.split("<sep>"):
                question = question.strip()
                if question and question not in ret:
                    ret.append(question)

        return ret

    def generate_batch(
        self, texts: Union[List[str], List[List[str]]], batch_size: Optional[int] = None
    ) -> Union[List[List[str]], List[List[List[str]]]]:
        """
        Generates questions for a list of strings or a list of lists of strings.

        :param texts: List of str or list of list of str.
        :param batch_size: Number of texts to process at a time.
        """

        if batch_size is None:
            batch_size = self.batch_size

        if isinstance(texts[0], str):
            single_doc_list = True
            number_of_docs = [1 for text_list in texts]
            text_iterator = texts
        else:
            single_doc_list = False
            number_of_docs = [len(text_list) for text_list in texts]
            text_iterator = itertools.chain.from_iterable(texts)  # type: ignore

        split_texts_docs = [
            self.preprocessor.split(
                document={"content": text},
                split_by="word",
                split_respect_sentence_boundary=False,
                split_overlap=self.split_overlap,
                split_length=self.split_length,
            )
            for text in text_iterator
        ]
        split_texts = [[doc.content for doc in split if isinstance(doc.content, str)] for split in split_texts_docs]
        number_of_splits = [len(split) for split in split_texts]
        flat_split_texts = [
            f"{self.prompt} {text}" if self.prompt not in text else text
            for text in itertools.chain.from_iterable(split_texts)
        ]

        batches = self._get_batches(flat_split_texts, batch_size=batch_size)
        all_string_outputs = []
        for batch in batches:
            tokenized = self.tokenizer(batch, return_tensors="pt", padding=True)
            input_ids = tokenized["input_ids"].to(self.devices[0])
            # Necessary if padding is enabled so the model won't attend pad tokens
            attention_mask = tokenized["attention_mask"].to(self.devices[0])
            tokens_output = self.model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                num_beams=self.num_beams,
                max_length=self.max_length,
                no_repeat_ngram_size=self.no_repeat_ngram_size,
                length_penalty=self.length_penalty,
                early_stopping=self.early_stopping,
                num_return_sequences=self.num_queries_per_doc,
            )

            string_output = self.tokenizer.batch_decode(tokens_output)
            string_output = [cur_output.replace("<pad>", "").replace("</s>", "") for cur_output in string_output]
            all_string_outputs.extend(string_output)

        # Group predictions together by split
        grouped_predictions_split = []
        left_idx = 0
        right_idx = 0
        for number in number_of_splits:
            right_idx = left_idx + number
            grouped_predictions_split.append(all_string_outputs[left_idx:right_idx])
            left_idx = right_idx
        # Group predictions together by doc list
        grouped_predictions_doc_list = []
        left_idx = 0
        right_idx = 0
        for number in number_of_docs:
            right_idx = left_idx + number
            grouped_predictions_doc_list.append(grouped_predictions_split[left_idx:right_idx])
            left_idx = right_idx

        results = []
        for group in grouped_predictions_doc_list:
            group_preds = []
            for doc in group:
                doc_preds = []
                for split in doc:
                    for question in split.split("<sep>"):
                        question = question.strip()
                        if question and question not in doc_preds:
                            doc_preds.append(question)
                group_preds.append(doc_preds)
            if single_doc_list:
                results.append(group_preds[0])
            else:
                results.append(group_preds)

        return results

    @staticmethod
    def _get_batches(texts: List[str], batch_size: Optional[int]) -> Iterator[List[str]]:
        if batch_size is None:
            yield texts
            return
        else:
            for index in range(0, len(texts), batch_size):
                yield texts[index : index + batch_size]
