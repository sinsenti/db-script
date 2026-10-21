//! Private worker control channel for best-effort PostgreSQL read cancellation.
//! The UI sends request IDs, never connection credentials or backend PIDs.
use postgres::{CancelToken, Client, NoTls};
use std::{
    io::{Read, Write},
    os::{
        fd::{FromRawFd, RawFd},
        unix::net::UnixStream,
    },
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
};

static ENABLED: AtomicBool = AtomicBool::new(false);
static STATE: Mutex<State> = Mutex::new(State {
    id: None,
    pending_cancel: None,
    cancelled: false,
    token: None,
});
struct State {
    id: Option<u64>,
    pending_cancel: Option<u64>,
    cancelled: bool,
    token: Option<CancelToken>,
}

impl State {
    fn start(&mut self, id: Option<u64>) {
        self.id = id;
        self.cancelled = id.is_some() && self.pending_cancel == id;
        self.token = None;
    }
    fn cancel(&mut self, id: u64) -> Option<CancelToken> {
        self.pending_cancel = Some(id);
        if self.id == Some(id) {
            self.cancelled = true;
            self.token.clone()
        } else {
            None
        }
    }
}

pub fn is_read_operation(op: &str) -> bool {
    ![
        "insert",
        "insert_row",
        "update",
        "update_row",
        "delete",
        "delete_row",
        "apply_batch",
        "bulk",
    ]
    .contains(&op)
}

/// Called once, before processing PostgreSQL protocol requests. The descriptor is
/// an explicitly inherited private Unix socket; unrelated inherited env is absent.
pub fn install() -> Result<(), &'static str> {
    let fd = match std::env::var("DBCLI_WORKER_CANCEL_FD") {
        Err(std::env::VarError::NotPresent) => return Ok(()),
        Ok(value) => value
            .parse::<RawFd>()
            .ok()
            .filter(|fd| *fd >= 3)
            .ok_or("Invalid worker cancellation descriptor")?,
        Err(_) => return Err("Invalid worker cancellation descriptor"),
    };
    let mut kind: libc::c_int = 0;
    let mut size = std::mem::size_of_val(&kind) as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_TYPE,
            &mut kind as *mut _ as *mut libc::c_void,
            &mut size,
        )
    } != 0
        || kind != libc::SOCK_STREAM
    {
        return Err("Invalid worker cancellation socket");
    }
    // Ownership of this inherited socket is transferred to the watcher thread.
    let mut socket = unsafe { UnixStream::from_raw_fd(fd) };
    socket
        .local_addr()
        .map_err(|_| "Cancellation socket must be Unix domain")?;
    if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
        return Err("Cannot protect cancellation descriptor");
    }
    if ENABLED.swap(true, Ordering::AcqRel) {
        return Err("Worker cancellation already installed");
    }
    std::thread::Builder::new()
        .name("postgres-read-cancel".into())
        .spawn(move || {
            let mut frame = [0u8; 8];
            while socket.read_exact(&mut frame).is_ok() {
                let id = u64::from_le_bytes(frame);
                let token = {
                    let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
                    state.cancel(id)
                };
                if let Some(token) = token {
                    // The driver sends PostgreSQL CancelRequest using its secret token.
                    // PostgreSQL does not acknowledge cancellation success. The UI has
                    // its own short grace window and will discard the worker regardless.
                    let _ = token.cancel_query(NoTls);
                }
                if socket.write_all(&frame).is_err() {
                    break;
                }
            }
        })
        .map_err(|_| "Cannot start worker cancellation watcher")?;
    Ok(())
}

pub struct Operation {
    id: Option<u64>,
}
pub fn begin(id: Option<u64>, read: bool) -> Operation {
    let id = id.filter(|_| read && ENABLED.load(Ordering::Acquire));
    let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    state.start(id);
    Operation { id }
}
impl Operation {
    pub fn cancelled(&self) -> bool {
        self.id.is_some_and(|id| {
            let state = STATE.lock().unwrap_or_else(|e| e.into_inner());
            state.id == Some(id) && state.cancelled
        })
    }
}
impl Drop for Operation {
    fn drop(&mut self) {
        let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
        if state.id == self.id {
            state.start(None);
        }
    }
}
/// Register only freshly connected READ ONLY clients. Writable clients never
/// expose a cancel token to this channel, even if a late/misrouted ID is received.
pub fn register_read(client: &Client) -> Result<(), ()> {
    if !ENABLED.load(Ordering::Acquire) {
        return Ok(());
    }
    let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    if state.id.is_some() {
        if state.cancelled {
            return Err(());
        }
        state.token = Some(client.cancel_token());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unrelated_ids_never_cancel_the_active_read() {
        let mut state = State {
            id: None,
            pending_cancel: None,
            cancelled: false,
            token: None,
        };
        state.start(Some(2));
        state.cancel(1);
        assert!(!state.cancelled);
        state.cancel(3);
        assert!(!state.cancelled);
        state.start(Some(3)); // cancellation can precede connection registration
        assert!(state.cancelled);
        state.cancel(1); // late IDs do not undo cancellation of the current request
        assert!(state.cancelled);
        state.start(None); // writes never have a read ID
        state.cancel(4);
        assert!(!state.cancelled);
    }
    #[test]
    fn mutations_never_register_for_read_cancellation() {
        for op in [
            "insert",
            "insert_row",
            "update",
            "update_row",
            "delete",
            "delete_row",
            "apply_batch",
            "bulk",
        ] {
            assert!(!is_read_operation(op));
        }
        for op in ["raw", "page", "count", "schema", "list_tables"] {
            assert!(is_read_operation(op));
        }
    }
}
