import atexit
import gzip
import logging
import os
from argparse import ArgumentParser
from pathlib import Path
from random import random
from threading import Thread
from time import sleep, time
from typing import Iterable, Optional, Protocol, TypeVar

import pkg_resources
import requests
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans
from uvicorn import Config, Server

from phoenix.config import (
    EXPORT_DIR,
    get_env_host,
    get_env_port,
    get_pids_path,
    get_working_dir,
)
from phoenix.core.model_schema_adapter import create_model_from_datasets
from phoenix.core.traces import Traces
from phoenix.datasets.dataset import EMPTY_DATASET, Dataset
from phoenix.datasets.fixtures import FIXTURES, get_datasets
from phoenix.db.engines import aiosqlite_engine, get_db_url
from phoenix.pointcloud.umap_parameters import (
    DEFAULT_MIN_DIST,
    DEFAULT_N_NEIGHBORS,
    DEFAULT_N_SAMPLES,
    UMAPParameters,
)
from phoenix.server.app import create_app
from phoenix.storage.span_store import SpanStore
from phoenix.trace.fixtures import (
    TRACES_FIXTURES,
    _download_traces_fixture,
    _get_trace_fixture_by_name,
    get_evals_from_fixture,
)
from phoenix.trace.otel import decode, encode
from phoenix.trace.schemas import Span
from phoenix.trace.span_json_decoder import json_string_to_span
from phoenix.utilities.span_store import get_span_store, load_traces_data_from_store

logger = logging.getLogger(__name__)

_WELCOME_MESSAGE = """

██████╗ ██╗  ██╗ ██████╗ ███████╗███╗   ██╗██╗██╗  ██╗
██╔══██╗██║  ██║██╔═══██╗██╔════╝████╗  ██║██║╚██╗██╔╝
██████╔╝███████║██║   ██║█████╗  ██╔██╗ ██║██║ ╚███╔╝
██╔═══╝ ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║██║ ██╔██╗
██║     ██║  ██║╚██████╔╝███████╗██║ ╚████║██║██╔╝ ██╗
╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝╚═╝╚═╝  ╚═╝ v{version}

|
|  🌎 Join our Community 🌎
|  https://join.slack.com/t/arize-ai/shared_invite/zt-1px8dcmlf-fmThhDFD_V_48oU7ALan4Q
|
|  ⭐️ Leave us a Star ⭐️
|  https://github.com/Arize-ai/phoenix
|
|  📚 Documentation 📚
|  https://docs.arize.com/phoenix
|
|  🚀 Phoenix Server 🚀
|  Phoenix UI: http://{host}:{port}
|  Log traces: /v1/traces over HTTP
|  Storage: {storage}
"""


def _write_pid_file_when_ready(
    server: Server,
    wait_up_to_seconds: float = 5,
) -> None:
    """Write PID file after server is started (or when time is up)."""
    time_limit = time() + wait_up_to_seconds
    while time() < time_limit and not server.should_exit and not server.started:
        sleep(1e-3)
    if time() >= time_limit and not server.started:
        server.should_exit = True
    _get_pid_file().touch()


def _remove_pid_file() -> None:
    _get_pid_file().unlink(missing_ok=True)


def _get_pid_file() -> Path:
    return get_pids_path() / str(os.getpid())


_Item = TypeVar("_Item", contravariant=True)


class _SupportsPut(Protocol[_Item]):
    def put(self, item: _Item) -> None: ...


def _load_items(
    queue: _SupportsPut[_Item],
    items: Iterable[_Item],
    simulate_streaming: Optional[bool] = False,
) -> None:
    for item in items:
        if simulate_streaming:
            sleep(random())
        queue.put(item)


def _send_spans(spans: Iterable[Span], url: str) -> None:
    # TODO(persistence): Ingest fixtures without networking for read-only deployments
    sleep(5)  # Wait for the server to start
    session = requests.session()
    for span in spans:
        req = ExportTraceServiceRequest(
            resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[encode(span)])])]
        )
        session.post(
            url=url,
            headers={
                "content-type": "application/x-protobuf",
                "content-encoding": "gzip",
            },
            data=gzip.compress(req.SerializeToString()),
        )
        # TODO(persistence): If ingestion rate is too high it can crash the UI, because
        # sqlite is not designed for high concurrency, especially for disk
        # persistence.
        sleep(0.2)


DEFAULT_UMAP_PARAMS_STR = f"{DEFAULT_MIN_DIST},{DEFAULT_N_NEIGHBORS},{DEFAULT_N_SAMPLES}"

if __name__ == "__main__":
    primary_dataset_name: str
    reference_dataset_name: Optional[str]
    trace_dataset_name: Optional[str] = None
    simulate_streaming: Optional[bool] = None

    primary_dataset: Dataset = EMPTY_DATASET
    reference_dataset: Optional[Dataset] = None
    corpus_dataset: Optional[Dataset] = None

    # automatically remove the pid file when the process is being gracefully terminated
    atexit.register(_remove_pid_file)

    parser = ArgumentParser()
    parser.add_argument("--export_path")
    parser.add_argument("--host", type=str, required=False)
    parser.add_argument("--port", type=int, required=False)
    parser.add_argument("--read-only", type=bool, default=False)
    parser.add_argument("--no-internet", action="store_true")
    parser.add_argument("--umap_params", type=str, required=False, default=DEFAULT_UMAP_PARAMS_STR)
    parser.add_argument("--debug", action="store_false")
    parser.add_argument("--enable-prometheus", type=bool, default=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    datasets_parser = subparsers.add_parser("datasets")
    datasets_parser.add_argument("--primary", type=str, required=True)
    datasets_parser.add_argument("--reference", type=str, required=False)
    datasets_parser.add_argument("--corpus", type=str, required=False)
    datasets_parser.add_argument("--trace", type=str, required=False)
    fixture_parser = subparsers.add_parser("fixture")
    fixture_parser.add_argument("fixture", type=str, choices=[fixture.name for fixture in FIXTURES])
    fixture_parser.add_argument("--primary-only", type=bool)
    trace_fixture_parser = subparsers.add_parser("trace-fixture")
    trace_fixture_parser.add_argument(
        "fixture", type=str, choices=[fixture.name for fixture in TRACES_FIXTURES]
    )
    trace_fixture_parser.add_argument("--simulate-streaming", type=bool)
    demo_parser = subparsers.add_parser("demo")
    demo_parser.add_argument("fixture", type=str, choices=[fixture.name for fixture in FIXTURES])
    demo_parser.add_argument(
        "trace_fixture", type=str, choices=[fixture.name for fixture in TRACES_FIXTURES]
    )
    demo_parser.add_argument("--simulate-streaming", action="store_true")
    args = parser.parse_args()
    export_path = Path(args.export_path) if args.export_path else EXPORT_DIR
    span_store: Optional[SpanStore] = None
    if args.command == "datasets":
        primary_dataset_name = args.primary
        reference_dataset_name = args.reference
        corpus_dataset_name = args.corpus
        primary_dataset = Dataset.from_name(primary_dataset_name)
        reference_dataset = (
            Dataset.from_name(reference_dataset_name)
            if reference_dataset_name is not None
            else None
        )
        corpus_dataset = (
            None if corpus_dataset_name is None else Dataset.from_name(corpus_dataset_name)
        )
    elif args.command == "fixture":
        fixture_name = args.fixture
        primary_only = args.primary_only
        primary_dataset, reference_dataset, corpus_dataset = get_datasets(
            fixture_name,
            args.no_internet,
        )
        if primary_only:
            reference_dataset_name = None
            reference_dataset = None
    elif args.command == "trace-fixture":
        trace_dataset_name = args.fixture
        simulate_streaming = args.simulate_streaming
    elif args.command == "demo":
        fixture_name = args.fixture
        primary_dataset, reference_dataset, corpus_dataset = get_datasets(
            fixture_name,
            args.no_internet,
        )
        trace_dataset_name = args.trace_fixture
        simulate_streaming = args.simulate_streaming

    host = args.host or get_env_host()
    port = args.port or get_env_port()

    model = create_model_from_datasets(
        primary_dataset,
        reference_dataset,
    )

    traces = Traces()
    if span_store := get_span_store():
        Thread(target=load_traces_data_from_store, args=(traces, span_store), daemon=True).start()
    if trace_dataset_name is not None:
        fixture_spans = list(
            # Apply `encode` here because legacy jsonl files contains UUIDs as strings.
            # `encode` removes the hyphens in the UUIDs.
            decode(encode(json_string_to_span(json_span)))
            for json_span in _download_traces_fixture(
                _get_trace_fixture_by_name(trace_dataset_name)
            )
        )
        Thread(
            target=_load_items,
            args=(traces, fixture_spans, simulate_streaming),
            daemon=True,
        ).start()
        Thread(
            target=_send_spans,
            args=(fixture_spans, f"http://{host}:{port}/v1/traces"),
            daemon=True,
        ).start()
        fixture_evals = list(get_evals_from_fixture(trace_dataset_name))
        Thread(
            target=_load_items,
            args=(traces, fixture_evals, simulate_streaming),
            daemon=True,
        ).start()
    umap_params_list = args.umap_params.split(",")
    umap_params = UMAPParameters(
        min_dist=float(umap_params_list[0]),
        n_neighbors=int(umap_params_list[1]),
        n_samples=int(umap_params_list[2]),
    )
    read_only = args.read_only
    logger.info(f"Server umap params: {umap_params}")
    if enable_prometheus := args.enable_prometheus:
        from phoenix.server.prometheus import start_prometheus

        start_prometheus()

    working_dir = get_working_dir().resolve()
    sql_url = get_db_url(database=working_dir / "phoenix.db")
    engine = aiosqlite_engine(working_dir / "phoenix.db")
    app = create_app(
        engine=engine,
        export_path=export_path,
        model=model,
        umap_params=umap_params,
        traces=traces,
        corpus=None if corpus_dataset is None else create_model_from_datasets(corpus_dataset),
        debug=args.debug,
        read_only=read_only,
        span_store=span_store,
        enable_prometheus=enable_prometheus,
    )
    server = Server(config=Config(app, host=host, port=port))
    Thread(target=_write_pid_file_when_ready, args=(server,), daemon=True).start()

    # Print information about the server
    phoenix_version = pkg_resources.get_distribution("arize-phoenix").version
    config = {
        "version": phoenix_version,
        "host": host,
        "port": port,
        "storage": sql_url,
    }
    print(_WELCOME_MESSAGE.format(**config))

    # Start the server
    server.run()
