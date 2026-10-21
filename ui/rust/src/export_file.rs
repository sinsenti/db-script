//! Exclusive, private export destinations with cleanup on a failed write.
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

struct Pending {
    path: PathBuf,
    file: File,
    complete: bool,
}
impl Drop for Pending {
    fn drop(&mut self) {
        if !self.complete {
            // Do not follow a symlink or remove a destination replaced by another
            // process while this export was being written.
            if let (Ok(opened), Ok(current)) =
                (self.file.metadata(), fs::symlink_metadata(&self.path))
            {
                if current.is_file()
                    && opened.dev() == current.dev()
                    && opened.ino() == current.ino()
                {
                    let _ = fs::remove_file(&self.path);
                }
            }
        }
    }
}

pub fn write_new<T>(path: &Path, write: impl FnOnce(&mut File) -> io::Result<T>) -> io::Result<T> {
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    let mut pending = Pending {
        path: path.into(),
        file,
        complete: false,
    };
    let value = write(&mut pending.file)?;
    pending.file.flush()?;
    pending.complete = true;
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        os::unix::fs::{symlink, PermissionsExt},
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn private_new_files_never_overwrite_and_failures_leave_no_partial_file() {
        let directory = std::env::temp_dir().join(format!(
            "dbcli-export-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("rows.json");
        write_new(&path, |file| file.write_all(b"original")).unwrap();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            write_new(&path, |file| file.write_all(b"replacement"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::AlreadyExists
        );
        assert_eq!(fs::read(&path).unwrap(), b"original");
        let link = directory.join("link.json");
        symlink(&path, &link).unwrap();
        assert!(write_new(&link, |file| file.write_all(b"replacement")).is_err());
        assert_eq!(fs::read(&path).unwrap(), b"original");
        let dangling = directory.join("dangling.json");
        symlink(directory.join("absent"), &dangling).unwrap();
        assert!(write_new(&dangling, |_| Ok(())).is_err());
        assert!(!directory.join("absent").exists());
        let partial = directory.join("partial.json");
        let result: io::Result<()> = write_new(&partial, |file| {
            file.write_all(b"partial")?;
            Err(io::Error::other("synthetic failure"))
        });
        assert!(result.is_err());
        assert!(!partial.exists());
        fs::remove_dir_all(directory).unwrap();
    }
}
