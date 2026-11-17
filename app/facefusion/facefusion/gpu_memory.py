"""Recognize recoverable GPU allocation failures without treating RAM/IO as VRAM."""
import traceback


class GpuMemoryError(RuntimeError):
    def __init__(self, message, workers=None):
        super().__init__(message)
        self.workers = dict(workers) if workers else None


def is_gpu_memory_error(error, providers=()):
    cuda = any('cuda' in str(provider).lower() for provider in (providers or ()))
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, GpuMemoryError):
            return True
        # Python/NumPy failures are host RAM; IO/FFmpeg failures are not ORT
        # GPU allocations, even if their stderr happens to say "out of memory".
        if not isinstance(error, (MemoryError, OSError)):
            message = str(error).lower()
            if any(marker in message for marker in (
                'cuda_error_out_of_memory', 'cudaerrormemoryallocation',
                'cudnn_status_alloc_failed', 'cublas_status_alloc_failed',
            )):
                return True
            host_failure = any(marker in message for marker in ('cpuallocator', 'cpu allocator', 'std::bad_alloc', 'host memory'))
            if not host_failure and ('cuda' in message or cuda) and 'out of memory' in message:
                return True
            if not host_failure and cuda and ('bfc_arena' in message or 'bfcarena' in message) and 'failed to allocate memory' in message:
                return True
        error = error.__cause__ or error.__context__
    return False


def forget_exception_frames(error):
    """Drop worker tracebacks before retry: they can retain entire ORT sessions."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend([current.__cause__, current.__context__])
        if current.__traceback__:
            traceback.clear_frames(current.__traceback__)
        current.__traceback__ = None
        current.__cause__ = None
        current.__context__ = None
