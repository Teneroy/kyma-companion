import typing
from typing import Protocol

import yaml
from langchain_core.prompts import PromptTemplate

from agents.common.data import Message
from initial_questions.output_parser import QuestionOutputParser
from initial_questions.prompts import INITIAL_QUESTIONS_PROMPT
from services.k8s import IK8sClient
from utils.logging import get_logger
from utils.models import IModel
from utils.utils import is_empty_str, is_non_empty_str

logger = get_logger(__name__)


class IInitialQuestionsHandler(Protocol):
    """Protocol for InitialQuestionsHandler."""

    def generate_questions(self, context: str) -> list[str]:
        """Generates initial questions given a context with cluster data."""
        ...

    def fetch_relevant_data_from_k8s_cluster(
        self, message: Message, k8s_client: IK8sClient
    ) -> str:
        """Fetch the relevant data from Kubernetes cluster based on specified K8s resource in message."""
        ...


class InitialQuestionsHandler:
    """Handler that generates initial questions."""

    chain: typing.Any

    def __init__(
        self,
        model: IModel,
    ) -> None:
        prompt_template: str = INITIAL_QUESTIONS_PROMPT
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=["context"],
        )
        output_parser = QuestionOutputParser()
        self.chain = prompt | model.llm | output_parser

    def generate_questions(self, context: str) -> list[str]:
        """Generates initial questions given a context with cluster data."""
        # Format prompt and send to llm.
        return self.chain.invoke({"context": context})  # type: ignore

    def fetch_relevant_data_from_k8s_cluster(
        self, message: Message, k8s_client: IK8sClient
    ) -> str:
        """Fetch the relevant data from Kubernetes cluster based on specified K8s resource in message."""

        logger.info("Fetching relevant data from k8s cluster")

        namespace: str = message.namespace or ""
        kind: str = message.resource_kind or ""
        name: str = message.resource_name or ""
        api_version: str = message.resource_api_version or ""

        # Query the Kubernetes API to get the context.
        context = ""

        if is_empty_str(namespace) and kind.lower() == "cluster":
            # Get an overview of the cluster
            # by fetching all not running pods, all K8s Nodes metrics,
            # and all K8s events with warning type.
            logger.info(
                "Fetching all not running Pods, Node metrics, and K8s Events with warning type"
            )
            pods = yaml.dump_all(k8s_client.list_not_running_pods(namespace=namespace))
            metrics = yaml.dump_all(k8s_client.list_nodes_metrics())
            events = yaml.dump_all(
                k8s_client.list_k8s_warning_events(namespace=namespace)
            )

            context = f"{pods}\n{metrics}\n{events}"

        elif is_non_empty_str(namespace) and kind.lower() == "namespace":
            # Get an overview of the namespace
            # by fetching all K8s events with warning type.
            logger.info("Fetching all K8s Events with warning type")
            context = yaml.dump_all(
                k8s_client.list_k8s_warning_events(namespace=namespace)
            )

        elif is_non_empty_str(kind) and is_non_empty_str(api_version):
            # Describe a specific resource. Not-namespaced resources need the namespace
            # field to be empty. Finally, get all events related to given resource.
            logger.info(
                f"Fetching all entities of Kind {kind} with API version {api_version}"
            )
            resources = yaml.dump(
                k8s_client.describe_resource(
                    api_version=api_version,
                    kind=kind,
                    name=name,
                    namespace=namespace,
                )
            )
            events = yaml.dump_all(
                k8s_client.list_k8s_events_for_resource(
                    kind=kind,
                    name=name,
                    namespace=namespace,
                )
            )

            context = f"{resources}\n{events}"

        else:
            raise Exception("Invalid message provided.")

        return context
