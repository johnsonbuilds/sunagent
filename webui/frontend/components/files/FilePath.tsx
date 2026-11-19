import { basename, dirname } from "@/lib/files";

export default function FilePath({ path }: { path: string }) {
  const dir = dirname(path);
  const base = basename(path);
  return (
    <span className="files-path" title={path}>
      {dir ? (
        <>
          <span className="files-path-dir">{dir}</span>
          <span className="files-path-sep">/</span>
        </>
      ) : null}
      <span className="files-path-base">{base}</span>
    </span>
  );
}
