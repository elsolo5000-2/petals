from __future__ import annotations

import multiprocessing as mp
import threading
from typing import Dict, Optional, Sequence, Union

import torch
from hivemind import DHT, BatchTensorDescriptor
from hivemind.moe.server.dht_handler import DHTHandlerThread
from hivemind.moe.server.layers import add_custom_models_from_file
from hivemind.moe.server.runtime import Runtime
from hivemind.proto.runtime_pb2 import CompressionType
from hivemind.utils.logging import get_logger, use_hivemind_log_handler

from src import BloomForCausalLM, DistributedBloomConfig
from src.bloom.block import BloomBlock
from src.server.backend import TransformerBackend
from src.server.cache import MemoryCache
from src.server.handler import TransformerConnectionHandler

use_hivemind_log_handler("in_root_logger")
logger = get_logger(__file__)


class Server(threading.Thread):
    """Serves one or more bloom layers for inference, forward and backward; announces oneself to the DHT"""

    def __init__(
        self,
        dht: DHT,
        module_backends: Dict[str, TransformerBackend],
        *,
        device: torch.device,
        num_connection_handlers: int = 8,
        update_period: float = 30,
        expiration: Optional[float] = None,
        start: bool,
        **kwargs,
    ):
        threading.Thread.__init__(self)
        self.dht, self.module_backends, self.update_period = dht, module_backends, update_period
        self.conn_handlers = [
            TransformerConnectionHandler(dht, self.module_backends) for _ in range(num_connection_handlers)
        ]
        self.runtime = Runtime(self.module_backends, device=device, **kwargs)
        self.dht_handler_thread = DHTHandlerThread(self.module_backends, dht, update_period, expiration, daemon=True)
        self.checkpoint_saver = None  # no need to save checkpoints since we do not change model state

        if start:
            self.run_in_background(await_ready=True)

    def run(self):
        """
        Starts Server in the current thread. Initializes dht if necessary, starts connection handlers,
        runs Runtime (self.runtime) to process incoming requests.
        """
        logger.info(f"Serving {len(self.module_backends)} blocks:")
        for expert_name, backend in self.module_backends.items():
            num_parameters = sum(p.numel() for p in backend.module.parameters() if p.requires_grad)
            logger.info(f"{expert_name}: {backend.module.__class__.__name__}, {num_parameters} parameters")

        if not self.dht.is_alive():
            self.dht.run_in_background(await_ready=True)

        if self.module_backends:
            self.dht_handler_thread.start()

        if self.checkpoint_saver is not None:
            self.checkpoint_saver.start()

        for process in self.conn_handlers:
            if not process.is_alive():
                process.start()
            process.ready.result()

        try:
            self.runtime.run()
        finally:
            self.shutdown()

    # noinspection PyMethodOverriding
    @classmethod
    def create(
        cls,
        prefix: str,
        block_config: str,
        num_blocks: Optional[int] = None,
        block_indices: Optional[str] = None,
        num_handlers: Optional[int] = None,
        min_batch_size: int = 1,
        max_batch_size: int = 4096,
        cache_size_bytes: Optional[int] = None,
        device: Union[str, torch.device] = None,
        initial_peers: Sequence[str] = (),
        compression=CompressionType.NONE,
        stats_report_interval: Optional[int] = None,
        custom_module_path=None,
        update_period: float = 30,
        expiration: Optional[float] = None,
        *,
        start: bool,
        **kwargs,
    ) -> Server:
        """Create a server with one or more bloom blocks. See run_server.py for documentation."""
        if custom_module_path is not None:
            add_custom_models_from_file(custom_module_path)
        assert (block_indices is None) != (num_blocks is None), "please specify num_blocks or block_indices, not both"
        dht = DHT(initial_peers=initial_peers, start=True, **kwargs)
        visible_maddrs_str = [str(a) for a in dht.get_visible_maddrs()]
        logger.info(f"Running DHT node on {visible_maddrs_str}, initial peers = {initial_peers}")

        device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        memory_cache = MemoryCache(device, cache_size_bytes)

        if block_indices is not None:
            try:
                start, end = block_indices.split(':')
                start, end = map(int, map(str.strip, (start, end)))
            except Exception as e:
                logger.error(f"Failed to parse --block_indices ({e}), must be start:end (e.g. 0:33)")
                raise
            block_indices = range(start, end)
        else:
            assert num_blocks is not None
            block_indices = range(num_blocks) # TODO replace with proper load balancing

        ## TODO: the code below will load the entire model in RAM. Please replace with sliced model
        block_config = DistributedBloomConfig.from_pretrained(block_config, use_auth_token=True)
        # model = BloomForCausalLM.from_pretrained(model, use_auth_token=True)
        ## /TODO


        # initialize modules
        blocks = {}
        for block_index in block_indices:
            module_uid = f"{prefix}.{block_index}"
            block = BloomBlock(block_config, layer_number=block_index)
            for param in block.parameters():
                param.requires_grad = False

            blocks[module_uid] = TransformerBackend(
                module_uid,
                block,
                memory_cache=memory_cache,
                args_schema=(BatchTensorDescriptor(1, 2048, block_config.hidden_size, compression=compression),),
                kwargs_schema={},
                outputs_schema=(BatchTensorDescriptor(1, 2048, block_config.hidden_size, compression=compression),),
                min_batch_size=min_batch_size,
                max_batch_size=max_batch_size,
            )

        num_handlers = num_handlers if num_handlers is not None else len(blocks) * 4

        return cls(
            dht,
            blocks,
            num_connection_handlers=num_handlers,
            device=device,
            stats_report_interval=stats_report_interval,
            update_period=update_period,
            expiration=expiration,
            start=start,
        )

    def run_in_background(self, await_ready=True, timeout=None):
        """
        Starts Server in a background thread. if await_ready, this method will wait until background server
        is ready to process incoming requests or for :timeout: seconds max.
        """
        self.start()
        if await_ready and not self.ready.wait(timeout=timeout):
            raise TimeoutError("Server didn't notify .ready in {timeout} seconds")

    @property
    def ready(self) -> mp.synchronize.Event:
        """
        An event (multiprocessing.Event) that is set when the server is ready to process requests.

        Example
        =======
        >>> server.start()
        >>> server.ready.wait(timeout=10)
        >>> print("Server ready" if server.ready.is_set() else "Server didn't start in 10 seconds")
        """
        return self.runtime.ready  # mp.Event that is true if self is ready to process batches

    def shutdown(self):
        """
        Gracefully terminate the server, process-safe.
        Please note that terminating server otherwise (e.g. by killing processes) may result in zombie processes.
        If you did already cause a zombie outbreak, your only option is to kill them with -9 (SIGKILL).
        """
        self.ready.clear()

        for process in self.conn_handlers:
            process.terminate()
            process.join()
        logger.debug("Connection handlers terminated")

        if self.module_backends:
            self.dht_handler_thread.stop.set()
            self.dht_handler_thread.join()

        if self.checkpoint_saver is not None:
            self.checkpoint_saver.stop.set()
            self.checkpoint_saver.join()

        self.dht.shutdown()
        self.dht.join()

        logger.debug(f"Shutting down runtime")

        self.runtime.shutdown()
        logger.info("Server shutdown succesfully")
