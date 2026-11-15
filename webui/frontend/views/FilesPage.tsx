"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { BranchIcon } from "@/components/Icons";
import ChangesTree from "@/components/files/ChangesTree";
import DiffView from "@/components/files/DiffView";
import FilesTabs, { type FilesTabId } from "@/components/files/FilesTabs";
import FilesToolbar from "@/components/files/FilesToolbar";
import FileTree from "@/components/files/FileTree";
import FilePreview from "@/components/files/FilePreview";
import { useShell } from "@/components/shell/AppShell";
import {
  ancestorDirs,
  buildChangeTree,
  changeTreeDirs,
  dirname,
} from "@/lib/files";
import { useI18n } from "@/lib/i18n";
import {
  useChanges,
  useFileDiff,
  useFilePreview,
  useFileTree,
} from "@/lib/useFiles";
import type { ChangeEntry, WorkspaceEntry } from "@/lib/types";

function readLocation(): { tab: FilesTabId; path: string } {
  const params = new URLSearchParams(window.location.search);
  return {
    tab: params.get("tab") === "changes" ? "changes" : "files",
    path: params.get("path") ?? "",
  };
}

function filesUrl(tab: FilesTabId, path: string): string {
  const params = new URLSearchParams();
  if (tab !== "files") params.set("tab", tab);
  if (path) params.set("path", path);
  const query = params.toString();
  return query ? `/files?${query}` : "/files";
}

export default function FilesPage() {
  const { t } = useI18n();
  const shell = useShell();
  const router = useRouter();

  const [tab, setTab] = useState<FilesTabId>("files");
  const [includeIgnored, setIncludeIgnored] = useState(false);
  const [filesPath, setFilesPath] = useState("");
  const [selectedFile, setSelectedFile] = useState("");
  const [protectedPath, setProtectedPath] = useState("");
  const [changesPath, setChangesPath] = useState("");
  const [diffProtected, setDiffProtected] = useState("");
  const [changeExpanded, setChangeExpanded] = useState<string[]>([]);
  const [previewVersion, setPreviewVersion] = useState(0);
  const [diffVersion, setDiffVersion] = useState(0);

  const tree = useFileTree(includeIgnored);
  const changes = useChanges();
  const preview = useFilePreview(selectedFile || null, previewVersion);

  const changeEntry = useMemo<ChangeEntry | null>(() => {
    if (!changesPath) return null;
    return (
      changes.data?.entries.find((entry) => entry.path === changesPath) ?? null
    );
  }, [changes.data, changesPath]);

  const diff = useFileDiff(
    diffProtected ? null : changesPath || null,
    changeEntry?.old_path ?? null,
    diffVersion,
  );

  const changeNodes = useMemo(
    () => buildChangeTree(changes.data?.entries ?? []),
    [changes.data],
  );

  const write = useCallback(
    (mode: "push" | "replace", nextTab: FilesTabId, path: string) => {
      router[mode](filesUrl(nextTab, path), { scroll: false });
    },
    [router],
  );

  const openFilesPath = useCallback(
    async (path: string) => {
      if (!path) {
        setFilesPath("");
        setSelectedFile("");
        setProtectedPath("");
        return;
      }
      const parent = dirname(path);
      const entries = await tree.loadDir(parent);
      const entry = entries?.find((item) => item.path === path);
      if (entry?.type === "dir") {
        await tree.expandTo(path);
        setFilesPath(path);
        setSelectedFile("");
        setProtectedPath("");
        return;
      }
      await tree.expandTo(parent);
      setFilesPath(path);
      setSelectedFile(entry?.protected ? "" : path);
      setProtectedPath(entry?.protected ? path : "");
    },
    [tree],
  );

  // Apply the URL once, then keep the browser history in step by hand
  // (the static export has no searchParams hook available during export).
  useEffect(() => {
    const initial = readLocation();
    setTab(initial.tab);
    if (initial.tab === "files") {
      void openFilesPath(initial.path);
    } else {
      setChangesPath(initial.path);
      setDiffProtected("");
      if (initial.path) setChangeExpanded(ancestorDirs(initial.path));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onPopState = () => {
      const next = readLocation();
      setTab(next.tab);
      if (next.tab === "files") {
        void openFilesPath(next.path);
      } else {
        setChangesPath(next.path);
        setDiffProtected("");
        setChangeExpanded(next.path ? ancestorDirs(next.path) : []);
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [openFilesPath]);

  // A protected file has no diff to fetch; the change list is what tells
  // a deep link that the selected entry is protected.
  useEffect(() => {
    if (changeEntry?.protected) setDiffProtected(changeEntry.path);
  }, [changeEntry]);

  // A deep link to a changed file should reveal its ancestor directories;
  // once the change list arrives, keep the URL selection expanded.
  useEffect(() => {
    if (tab !== "changes" || !changesPath || !changes.data) return;
    setChangeExpanded((current) => {
      if (current.length > 0) return current;
      return ancestorDirs(changesPath);
    });
  }, [tab, changesPath, changes.data]);

  const switchTab = (next: FilesTabId) => {
    if (next === tab) return;
    setTab(next);
    write("push", next, next === "files" ? filesPath : changesPath);
    if (next === "changes") {
      void changes.refresh(true);
      if (changesPath) setDiffVersion((value) => value + 1);
    }
  };

  const toggleDirectory = (path: string) => {
    tree.toggleDir(path);
    setFilesPath(path);
    setSelectedFile("");
    setProtectedPath("");
    // Keep the URL pointing at the directory, open or closed: a reload
    // reopens it instead of forgetting where the user was.
    write("replace", "files", path);
  };

  const selectFile = (entry: WorkspaceEntry) => {
    setFilesPath(entry.path);
    setProtectedPath(entry.protected ? entry.path : "");
    setSelectedFile(entry.protected ? "" : entry.path);
    if (!entry.protected && selectedFile === entry.path) {
      // Re-selecting the open file is the cheap way to pick up an edit.
      setPreviewVersion((value) => value + 1);
    }
    write("replace", "files", entry.path);
  };

  const selectChange = (entry: ChangeEntry) => {
    setChangesPath(entry.path);
    setDiffProtected(entry.protected ? entry.path : "");
    setChangeExpanded((current) => [
      ...new Set([...current, ...ancestorDirs(entry.path)]),
    ]);
    write("replace", "changes", entry.path);
  };

  const refresh = () => {
    if (tab === "files") {
      void tree.refreshVisible();
      if (selectedFile) setPreviewVersion((value) => value + 1);
      return;
    }
    void changes.refresh();
    if (changesPath) setDiffVersion((value) => value + 1);
  };

  const back = () => {
    if (tab === "files") {
      setSelectedFile("");
      setProtectedPath("");
      setFilesPath("");
      write("replace", "files", "");
      return;
    }
    setChangesPath("");
    setDiffProtected("");
    write("replace", "changes", "");
  };

  const openInFiles = () => {
    if (!changesPath) return;
    const path = changesPath;
    setTab("files");
    void openFilesPath(path).then(() => write("push", "files", path));
  };

  const toggleIgnored = () => {
    setIncludeIgnored((value) => !value);
    setSelectedFile("");
    setProtectedPath("");
    setFilesPath("");
    write("replace", "files", "");
  };

  const rootState = tree.dirs[""];
  const hiddenIgnored = rootState?.hiddenIgnored ?? 0;
  const narrowOpen =
    shell.narrow &&
    (tab === "files" ? selectedFile !== "" || protectedPath !== "" : changesPath !== "");

  const summary = changes.data;

  return (
    <div className="files-page">
      <header className="files-head">
        <FilesTabs
          tab={tab}
          changes={changes.data?.counts.total ?? 0}
          onChange={switchTab}
        />
        <FilesToolbar
          root={summary?.root ?? null}
          tab={tab}
          refreshing={
            tab === "files"
              ? tree.dirs[""]?.status === "loading"
              : changes.loading
          }
          includeIgnored={includeIgnored}
          hiddenIgnored={hiddenIgnored}
          onRefresh={refresh}
          onToggleIgnored={toggleIgnored}
          onExpandAll={() => setChangeExpanded(changeTreeDirs(changeNodes))}
          onCollapseAll={() => setChangeExpanded([])}
        />
      </header>

      <div className={`files-body${narrowOpen ? " is-narrow-open" : ""}`}>
        <aside className="files-tree-pane">
          {tab === "files" ? (
            <FileTree
              dirs={tree.dirs}
              expanded={tree.expanded}
              selected={selectedFile || protectedPath || null}
              onToggle={toggleDirectory}
              onSelect={selectFile}
              onRetry={(path) => void tree.loadDir(path, true)}
            />
          ) : (
            <>
              {summary ? (
                <div className="files-changes-summary">
                  <span className="files-summary-branch">
                    <BranchIcon />
                    {summary.is_git ? summary.branch ?? "—" : t("filesNotGitRepo")}
                  </span>
                  {summary.is_git ? (
                    <span className="files-summary-counts">
                      {summary.counts.total} · {t("filesTabChanges")}
                    </span>
                  ) : null}
                  {summary.ahead || summary.behind ? (
                    <span className="files-summary-upstream">
                      {t("filesAheadBehind", {
                        ahead: summary.ahead,
                        behind: summary.behind,
                      })}
                    </span>
                  ) : null}
                  {changes.updatedAt ? (
                    <span className="files-summary-time">
                      {new Date(changes.updatedAt).toLocaleTimeString()}
                    </span>
                  ) : null}
                </div>
              ) : null}

              {changes.loading && !changes.data ? (
                <div className="files-pane-message">{t("filesLoading")}</div>
              ) : changes.error && !changes.data ? (
                <div className="files-pane-error">
                  <p>{changes.error}</p>
                  <button
                    type="button"
                    className="files-btn"
                    onClick={() => void changes.refresh()}
                  >
                    {t("filesRetry")}
                  </button>
                </div>
              ) : summary && !summary.is_git ? (
                <div className="files-pane-empty">
                  <p>{t("filesNotGitRepo")}</p>
                </div>
              ) : (
                <>
                  {changes.error ? (
                    <div className="files-note is-warning">{changes.error}</div>
                  ) : null}
                  <ChangesTree
                    nodes={changeNodes}
                    expanded={changeExpanded}
                    selected={changesPath || null}
                    onRetry={() => void changes.refresh()}
                    onToggle={(path) =>
                      setChangeExpanded((current) =>
                        current.includes(path)
                          ? current.filter((item) => item !== path)
                          : [...current, path],
                      )
                    }
                    onSelect={selectChange}
                  />
                  {summary?.truncated ? (
                    <div className="files-note">
                      {t("filesChangesTruncated", {
                        count: summary.entries.length,
                      })}
                    </div>
                  ) : null}
                </>
              )}
            </>
          )}
        </aside>

        <section className="files-view-pane">
          {tab === "files" ? (
            <FilePreview
              path={selectedFile || protectedPath || null}
              data={protectedPath ? null : preview.data}
              loading={preview.loading}
              error={preview.error}
              changedOnDisk={preview.changedOnDisk}
              protectedPath={protectedPath || null}
              onBack={back}
              onRetry={() => setPreviewVersion((value) => value + 1)}
            />
          ) : (
            <DiffView
              path={changesPath || null}
              data={diff.data}
              entry={changeEntry}
              loading={diff.loading}
              error={diff.error}
              protectedPath={diffProtected || null}
              onRetry={() => setDiffVersion((value) => value + 1)}
              onBack={back}
              onOpenInFiles={openInFiles}
            />
          )}
        </section>
      </div>
    </div>
  );
}
