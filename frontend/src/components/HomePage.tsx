import { useEffect, useState, useRef } from 'react';
import {
  Plus,
  BookOpenText,
  Pencil,
  Trash2,
  ImagePlus,
  ChevronRight,
  X,
  Check,
  Sparkles,
  Users,
  Loader2,
  Download,
  Upload,
  FolderOpen,
  CalendarDays,
} from 'lucide-react';
import {
  listStories,
  createStory,
  updateStory,
  deleteStory,
  exportStory,
  importStoryPackage,
  uploadStoryCover,
  mangaThumbUrl,
  saveStoryCharacters,
  addStoryRefImage,
  deleteStoryRefImage,
  getStoryAssetGroups,
  createStoryAssetGroup,
  updateStoryAssetGroup,
  deleteStoryAssetGroup,
  addStoryAssetGroupRefImage,
  deleteStoryAssetGroupRefImage,
  type Story,
  type RefImage,
  type AssetGroup,
} from '../api';

interface Props {
  onSelectStory: (story: Story) => void;
}

export default function HomePage({ onSelectStory }: Props) {
  const [stories, setStories] = useState<Story[]>([]);
  const [loading, setLoading] = useState(true);

  // New story dialog
  const [showNew, setShowNew] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newDesc, setNewDesc] = useState('');

  // Edit mode
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editDesc, setEditDesc] = useState('');

  const fileRef = useRef<HTMLInputElement>(null);
  const importFileRef = useRef<HTMLInputElement>(null);
  const [uploadingCover, setUploadingCover] = useState<number | null>(null);
  const [importingStory, setImportingStory] = useState(false);
  const [importProgress, setImportProgress] = useState<{ message: string; percent?: number } | null>(null);
  const [exportingStoryId, setExportingStoryId] = useState<number | null>(null);

  // Character card modal
  const [charModalStoryId, setCharModalStoryId] = useState<number | null>(null);
  const [charModalText, setCharModalText] = useState('');
  const [charModalLoading] = useState(false);
  const [charModalSaving, setCharModalSaving] = useState(false);
  const [storyCharFlags, setStoryCharFlags] = useState<Record<number, boolean>>({});

  // Ref images modal (multi)
  const [refModalStoryId, setRefModalStoryId] = useState<number | null>(null);
  const [refModalImages, setRefModalImages] = useState<RefImage[]>([]);
  const [refModalMax, setRefModalMax] = useState(4);
  const [refModalLoading] = useState(false);
  const [refModalUploading, setRefModalUploading] = useState(false);
  const [storyRefFlags, setStoryRefFlags] = useState<Record<number, boolean>>({});
  const refModalFileRef = useRef<HTMLInputElement>(null);

  // Story global asset groups
  const [assetModalStoryId, setAssetModalStoryId] = useState<number | null>(null);
  const [assetGroups, setAssetGroups] = useState<AssetGroup[]>([]);
  const [assetSelectedKey, setAssetSelectedKey] = useState<string>('default');
  const [assetDraftName, setAssetDraftName] = useState('');
  const [assetDraftChars, setAssetDraftChars] = useState('');
  const [assetModalLoading, setAssetModalLoading] = useState(false);
  const [assetModalSaving, setAssetModalSaving] = useState(false);
  const [assetRefUploading, setAssetRefUploading] = useState(false);
  const assetModalRequestRef = useRef(0);
  const assetFileRef = useRef<HTMLInputElement>(null);

  const activeAssetGroup = assetGroups.find((g) => (g.id === null ? 'default' : String(g.id)) === assetSelectedKey) ?? assetGroups[0];
  const updateAssetFlags = (storyId: number, groups: AssetGroup[]) => {
    setStoryCharFlags((prev) => ({ ...prev, [storyId]: groups.some((g) => !!g.character_profiles?.trim()) }));
    setStoryRefFlags((prev) => ({ ...prev, [storyId]: groups.some((g) => g.ref_count > 0) }));
  };

  const syncActiveAssetDraft = (group: AssetGroup | undefined) => {
    setAssetDraftName(group?.name ?? '');
    setAssetDraftChars(group?.character_profiles ?? '');
  };

  const openAssetGroupModal = async (storyId: number) => {
    const requestId = ++assetModalRequestRef.current;
    setAssetModalStoryId(storyId);
    setAssetGroups([]);
    setAssetSelectedKey('default');
    setAssetModalLoading(true);
    try {
      const payload = await getStoryAssetGroups(storyId);
      if (assetModalRequestRef.current !== requestId) return;
      setAssetGroups(payload.groups);
      setRefModalMax(payload.max);
      syncActiveAssetDraft(payload.groups[0]);
      updateAssetFlags(storyId, payload.groups);
    } catch (err: any) {
      if (assetModalRequestRef.current !== requestId) return;
      alert(`加载设定组失败: ${err.message}`);
    } finally {
      if (assetModalRequestRef.current !== requestId) return;
      setAssetModalLoading(false);
    }
  };

  const selectAssetGroup = (group: AssetGroup) => {
    setAssetSelectedKey(group.id === null ? 'default' : String(group.id));
    syncActiveAssetDraft(group);
  };

  const replaceAssetGroup = (groupId: number | null, patch: Partial<AssetGroup>) => {
    setAssetGroups((prev) => prev.map((g) => (g.id === groupId ? { ...g, ...patch } : g)));
  };

  const saveAssetGroup = async () => {
    if (assetModalStoryId === null || !activeAssetGroup) return;
    setAssetModalSaving(true);
    try {
      if (activeAssetGroup.id === null) {
        await saveStoryCharacters(assetModalStoryId, assetDraftChars);
        const next = { ...activeAssetGroup, character_profiles: assetDraftChars, has_character_profiles: !!assetDraftChars.trim() };
        replaceAssetGroup(null, next);
        updateAssetFlags(assetModalStoryId, assetGroups.map((g) => (g.id === null ? next : g)));
      } else {
        const result = await updateStoryAssetGroup(assetModalStoryId, activeAssetGroup.id, {
          name: assetDraftName,
          characters: assetDraftChars,
        });
        setAssetGroups(result.groups);
        const next = result.groups.find((g) => g.id === activeAssetGroup.id);
        syncActiveAssetDraft(next);
        updateAssetFlags(assetModalStoryId, result.groups);
      }
    } catch (err: any) {
      alert(`保存设定组失败: ${err.message}`);
    } finally {
      setAssetModalSaving(false);
    }
  };

  const addAssetGroup = async () => {
    if (assetModalStoryId === null) return;
    try {
      const result = await createStoryAssetGroup(assetModalStoryId, `设定组 ${Math.max(assetGroups.length, 1)}`);
      setAssetGroups(result.groups);
      setAssetSelectedKey(String(result.group.id));
      syncActiveAssetDraft(result.group);
      updateAssetFlags(assetModalStoryId, result.groups);
    } catch (err: any) {
      alert(`新增设定组失败: ${err.message}`);
    }
  };

  const removeAssetGroup = async () => {
    if (assetModalStoryId === null || !activeAssetGroup?.id) return;
    if (!confirm(`删除「${activeAssetGroup.name}」？已选择该组的章节会恢复为默认组。`)) return;
    try {
      const result = await deleteStoryAssetGroup(assetModalStoryId, activeAssetGroup.id);
      setAssetGroups(result.groups);
      setAssetSelectedKey('default');
      syncActiveAssetDraft(result.groups[0]);
      updateAssetFlags(assetModalStoryId, result.groups);
    } catch (err: any) {
      alert(`删除设定组失败: ${err.message}`);
    }
  };

  const handleAssetRefUpload = async (file: File) => {
    if (assetModalStoryId === null || !activeAssetGroup) return;
    setAssetRefUploading(true);
    try {
      const reader = new FileReader();
      const b64 = await new Promise<string>((resolve) => {
        reader.onload = () => resolve((reader.result as string).split(',')[1]);
        reader.readAsDataURL(file);
      });
      const r = activeAssetGroup.id === null
        ? await addStoryRefImage(assetModalStoryId, b64)
        : await addStoryAssetGroupRefImage(assetModalStoryId, activeAssetGroup.id, b64);
      const patch = { ref_images: r.images, ref_count: r.images.length };
      replaceAssetGroup(activeAssetGroup.id, patch);
      updateAssetFlags(assetModalStoryId, assetGroups.map((g) => (g.id === activeAssetGroup.id ? { ...g, ...patch } : g)));
    } catch (err: any) {
      alert(`上传垫图失败: ${err.message}`);
    } finally {
      setAssetRefUploading(false);
    }
  };

  const handleAssetRefDelete = async (filename: string) => {
    if (assetModalStoryId === null || !activeAssetGroup) return;
    try {
      const r = activeAssetGroup.id === null
        ? await deleteStoryRefImage(assetModalStoryId, filename)
        : await deleteStoryAssetGroupRefImage(assetModalStoryId, activeAssetGroup.id, filename);
      const patch = { ref_images: r.images, ref_count: r.images.length };
      replaceAssetGroup(activeAssetGroup.id, patch);
      updateAssetFlags(assetModalStoryId, assetGroups.map((g) => (g.id === activeAssetGroup.id ? { ...g, ...patch } : g)));
    } catch (err: any) {
      alert(`删除垫图失败: ${err.message}`);
    }
  };

  const saveCharModal = async () => {
    if (charModalStoryId === null) return;
    setCharModalSaving(true);
    try {
      await saveStoryCharacters(charModalStoryId, charModalText);
      setStoryCharFlags((prev) => ({ ...prev, [charModalStoryId]: !!charModalText.trim() }));
      setCharModalStoryId(null);
    } catch (err: any) {
      alert(`保存失败: ${err.message}`);
    } finally {
      setCharModalSaving(false);
    }
  };

  const handleRefUpload = async (file: File) => {
    if (refModalStoryId === null) return;
    setRefModalUploading(true);
    try {
      const reader = new FileReader();
      const b64 = await new Promise<string>((resolve) => {
        reader.onload = () => resolve((reader.result as string).split(',')[1]);
        reader.readAsDataURL(file);
      });
      const r = await addStoryRefImage(refModalStoryId, b64);
      setRefModalImages(r.images);
      setRefModalMax(r.max);
      setStoryRefFlags((prev) => ({ ...prev, [refModalStoryId]: r.images.length > 0 }));
    } catch (err: any) {
      alert(`上传垫图失败: ${err.message}`);
    } finally {
      setRefModalUploading(false);
    }
  };

  const handleRefDelete = async (filename: string) => {
    if (refModalStoryId === null) return;
    try {
      const r = await deleteStoryRefImage(refModalStoryId, filename);
      setRefModalImages(r.images);
      setStoryRefFlags((prev) => ({ ...prev, [refModalStoryId]: r.images.length > 0 }));
    } catch (err: any) {
      alert(`删除垫图失败: ${err.message}`);
    }
  };

  useEffect(() => {
    loadStories();
  }, []);

  const loadStories = async () => {
    try {
      const list = await listStories();
      setStories(list);
      const charFlags = Object.fromEntries(
        list.map((s) => [s.id, !!s.has_character_profiles])
      );
      setStoryCharFlags(charFlags);
      const refFlags = Object.fromEntries(
        list.map((s) => [s.id, !!s.has_ref_image])
      );
      setStoryRefFlags(refFlags);
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async () => {
    const title = newTitle.trim() || '未命名故事';
    const desc = newDesc.trim();
    const s = await createStory(title, desc);
    setStories((prev) => [s, ...prev]);
    setStoryCharFlags((prev) => ({ ...prev, [s.id]: !!s.has_character_profiles }));
    setStoryRefFlags((prev) => ({ ...prev, [s.id]: !!s.has_ref_image }));
    setShowNew(false);
    setNewTitle('');
    setNewDesc('');
  };

  const handleDelete = async (id: number) => {
    if (!confirm('确定要删除这本小说吗？所有章节、对话、漫画都将被永久删除！')) return;
    await deleteStory(id);
    setStories((prev) => prev.filter((s) => s.id !== id));
  };

  const handleExport = async (s: Story) => {
    setExportingStoryId(s.id);
    try {
      await exportStory(s);
    } catch (err: any) {
      alert(`导出失败: ${err.message}`);
    } finally {
      setExportingStoryId(null);
    }
  };

  const handleImportFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setImportingStory(true);
    setImportProgress({ message: '准备上传作品包...', percent: 0 });
    try {
      const imported = await importStoryPackage(file, (progress) => {
        setImportProgress({ message: progress.message, percent: progress.percent });
      });
      await loadStories();
      onSelectStory(imported);
    } catch (err: any) {
      alert(`导入失败: ${err.message}`);
    } finally {
      setImportingStory(false);
      setImportProgress(null);
    }
  };

  const startEdit = (s: Story) => {
    setEditingId(s.id);
    setEditTitle(s.title);
    setEditDesc(s.description || '');
  };

  const saveEdit = async () => {
    if (editingId === null) return;
    const updated = await updateStory(editingId, {
      title: editTitle.trim() || '未命名故事',
      description: editDesc.trim(),
    });
    setStories((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
    setEditingId(null);
  };

  const handleCoverClick = (storyId: number) => {
    setUploadingCover(storyId);
    fileRef.current?.click();
  };

  const handleCoverFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || uploadingCover === null) return;
    const storyId = uploadingCover;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const b64 = (reader.result as string).split(',')[1];
        const coverPath = await uploadStoryCover(storyId, b64);
        setStories((prev) =>
          prev.map((s) => (s.id === storyId ? { ...s, cover_image: coverPath } : s))
        );
      } catch (err: any) {
        alert(`上传封面失败: ${err.message}`);
      } finally {
        setUploadingCover(null);
      }
    };
    reader.onerror = () => {
      alert('读取封面文件失败');
      setUploadingCover(null);
    };
    reader.readAsDataURL(file);
    e.target.value = '';
  };

  const formatDate = (value: string) =>
    new Date(value).toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-950 text-gray-400">
        <div className="flex flex-col items-center gap-3 rounded-2xl border border-gray-800 bg-gray-900/60 px-10 py-8 shadow-2xl">
          <Loader2 size={34} className="animate-spin text-violet-400" />
          <span className="text-sm">正在整理你的故事书架…</span>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={handleCoverFile}
      />
      <input
        ref={importFileRef}
        type="file"
        accept=".zip,application/zip"
        className="hidden"
        onChange={handleImportFile}
      />
      {importProgress && (
        <div className="fixed inset-x-0 top-4 z-[70] mx-auto w-[calc(100%-32px)] max-w-md rounded-2xl border border-gray-800 bg-gray-900/95 p-4 shadow-2xl backdrop-blur">
          <div className="mb-3 flex items-center gap-3 text-sm font-medium text-gray-100">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-violet-600/20 text-violet-400">
              <Loader2 size={17} className="animate-spin" />
            </span>
            <span className="leading-snug">{importProgress.message}</span>
          </div>
          <div className="h-2.5 overflow-hidden rounded-full bg-gray-800">
            <div
              className="h-full rounded-full bg-violet-500 transition-all duration-200"
              style={{ width: `${importProgress.percent ?? 100}%` }}
            />
          </div>
          <p className="mt-3 text-xs leading-relaxed text-gray-500">
            上传完成后服务器还需要解压图片并写入数据库，大作品会多等一会儿。
          </p>
        </div>
      )}

      {/* Header */}
      <header className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950/80 backdrop-blur-sm">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-4 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-violet-600 shadow-lg">
              <Sparkles size={18} className="text-white" />
            </div>
            <div className="min-w-0">
              <h1 className="truncate text-lg font-bold tracking-tight">AI-Cartoon</h1>
              <p className="text-xs text-gray-500">AI 小说 · 漫画工坊</p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              onClick={() => importFileRef.current?.click()}
              disabled={importingStory}
              className="flex items-center gap-2 rounded-xl border border-gray-800 bg-gray-900 px-3 py-2.5 text-sm font-medium text-gray-300 shadow-sm transition hover:-translate-y-0.5 hover:border-violet-500 hover:text-violet-400 disabled:opacity-40 sm:px-4"
              title="导入整本作品"
            >
              {importingStory ? <Loader2 size={16} className="animate-spin" /> : <Upload size={16} />}
              <span className="hidden sm:inline">导入</span>
            </button>
            <button
              onClick={() => setShowNew(true)}
              className="flex items-center gap-2 rounded-xl bg-violet-600 px-3 py-2.5 text-sm font-medium text-white shadow-lg transition hover:-translate-y-0.5 hover:bg-violet-500 sm:px-4"
            >
              <Plus size={16} />
              <span>新建小说</span>
            </button>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
        <section className="mb-8 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-violet-400">Story Library</p>
            <h2 className="text-2xl font-bold tracking-tight text-gray-100 sm:text-3xl">你的创作书架</h2>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-gray-500">
              管理小说、封面、角色设定组和参考图，进入作品后继续生成分镜与漫画。
            </p>
          </div>
          <div className="flex gap-3">
            <div className="rounded-2xl border border-gray-800 bg-gray-900 px-4 py-3 shadow-lg">
              <div className="text-xl font-bold text-gray-100">{stories.length}</div>
              <div className="text-xs text-gray-500">本地作品</div>
            </div>
          </div>
        </section>

        {/* New story dialog */}
        {showNew && (
          <div className="mb-8 rounded-2xl border border-gray-800 bg-gray-900 p-5 shadow-2xl sm:p-6">
            <div className="mb-5 flex items-center justify-between gap-3">
              <div>
                <h3 className="flex items-center gap-2 text-base font-semibold">
                  <Plus size={16} className="text-violet-400" />
                  创建新小说
                </h3>
                <p className="mt-1 text-xs text-gray-500">先写一个名字，描述可以稍后再补。</p>
              </div>
              <button
                onClick={() => setShowNew(false)}
                className="rounded-lg p-2 text-gray-500 transition hover:bg-gray-800 hover:text-gray-300"
                aria-label="关闭创建面板"
              >
                <X size={16} />
              </button>
            </div>
            <div className="space-y-3">
              <input
                autoFocus
                placeholder="小说名称"
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
                className="w-full rounded-xl border border-gray-700 bg-gray-800 px-4 py-3 text-sm placeholder-gray-500 transition focus:border-violet-500 focus:outline-none"
              />
              <textarea
                placeholder="简短描述（可选）"
                value={newDesc}
                onChange={(e) => setNewDesc(e.target.value)}
                rows={2}
                className="w-full resize-none rounded-xl border border-gray-700 bg-gray-800 px-4 py-3 text-sm placeholder-gray-500 transition focus:border-violet-500 focus:outline-none"
              />
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setShowNew(false)}
                  className="rounded-xl px-4 py-2 text-sm text-gray-400 transition-colors hover:bg-gray-800 hover:text-gray-200"
                >
                  取消
                </button>
                <button
                  onClick={handleCreate}
                  className="rounded-xl bg-violet-600 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-violet-500"
                >
                  创建
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Empty state */}
        {stories.length === 0 && !showNew && (
          <div className="flex flex-col items-center justify-center rounded-3xl border border-dashed border-gray-800 bg-gray-900/60 px-6 py-24 text-center text-gray-500 shadow-2xl">
            <div className="mb-5 flex h-20 w-20 items-center justify-center rounded-3xl bg-violet-600/20 text-violet-400">
              <BookOpenText size={38} />
            </div>
            <p className="mb-2 text-xl font-semibold text-gray-100">还没有小说</p>
            <p className="mb-7 max-w-md text-sm leading-relaxed">创建第一本小说，或者导入已有作品包，继续你的漫画宇宙。</p>
            <button
              onClick={() => setShowNew(true)}
              className="inline-flex items-center gap-2 rounded-xl bg-violet-600 px-5 py-2.5 text-sm font-medium text-white shadow-lg transition hover:-translate-y-0.5 hover:bg-violet-500"
            >
              <Plus size={16} />
              新建小说
            </button>
          </div>
        )}

        {/* Story cards grid */}
        {stories.length > 0 && (
          <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {stories.map((s) => (
              <div
                key={s.id}
                className="group overflow-hidden rounded-2xl border border-gray-800 bg-gray-900 shadow-lg transition-all duration-200 hover:-translate-y-1 hover:border-violet-500 hover:shadow-2xl"
              >
                {/* Cover */}
                <div
                  className="relative h-52 cursor-pointer overflow-hidden bg-gray-800"
                  onClick={() => handleCoverClick(s.id)}
                >
                  {s.cover_image ? (
                    <img
                      src={mangaThumbUrl(s.cover_image, 720)!}
                      alt={s.title}
                      className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
                      loading="lazy"
                      decoding="async"
                    />
                  ) : (
                    <div className="flex h-full flex-col items-center justify-center bg-gray-800 text-gray-600 transition-colors group-hover:text-gray-500">
                      <ImagePlus size={34} className="mb-2" />
                      <span className="text-xs">点击上传封面</span>
                    </div>
                  )}
                  <div className="absolute inset-x-0 bottom-0 h-20 bg-gradient-to-t from-black/45 to-transparent opacity-0 transition-opacity group-hover:opacity-100" />
                </div>

                {/* Info */}
                <div className="p-4">
                  {editingId === s.id ? (
                    <div className="space-y-2">
                      <input
                        autoFocus
                        value={editTitle}
                        onChange={(e) => setEditTitle(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && saveEdit()}
                        className="w-full rounded-xl border border-gray-700 bg-gray-800 px-3 py-2 text-sm focus:border-violet-500 focus:outline-none"
                      />
                      <textarea
                        value={editDesc}
                        onChange={(e) => setEditDesc(e.target.value)}
                        rows={2}
                        className="w-full resize-none rounded-xl border border-gray-700 bg-gray-800 px-3 py-2 text-sm focus:border-violet-500 focus:outline-none"
                        placeholder="简短描述（可选）"
                      />
                      <div className="flex justify-end gap-1">
                        <button
                          onClick={() => setEditingId(null)}
                          className="rounded-lg p-2 text-gray-500 hover:bg-gray-800 hover:text-gray-300"
                          title="取消编辑"
                        >
                          <X size={14} />
                        </button>
                        <button
                          onClick={saveEdit}
                          className="rounded-lg p-2 text-violet-400 hover:bg-violet-600/20"
                          title="保存编辑"
                        >
                          <Check size={14} />
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <h3 className="mb-1 line-clamp-1 text-base font-semibold text-gray-100">{s.title}</h3>
                      {s.description && (
                        <p className="mb-3 line-clamp-2 min-h-[2.5rem] text-xs leading-relaxed text-gray-500">{s.description}</p>
                      )}
                      {!s.description && <p className="mb-3 min-h-[2.5rem] text-xs text-gray-500">还没有描述</p>}
                      <div className="mb-4 flex items-center gap-1.5 text-xs text-gray-600">
                        <CalendarDays size={13} />
                        <span>{formatDate(s.created_at)}</span>
                      </div>
                      <div className="flex items-center justify-between gap-2">
                        <span className="inline-flex items-center gap-1 rounded-full border border-gray-800 bg-gray-800 px-2.5 py-1 text-[11px] text-gray-500">
                          <FolderOpen size={12} />
                          作品
                        </span>
                        <div className="flex items-center gap-1.5">
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              openAssetGroupModal(s.id);
                            }}
                            className={`rounded-lg p-1.5 transition-colors ${
                              storyCharFlags[s.id]
                                ? 'text-emerald-400 hover:text-emerald-300'
                                : 'text-gray-600 hover:bg-gray-800 hover:text-gray-300'
                            }`}
                            title={storyCharFlags[s.id] ? '角色卡（已设定）' : '设置角色卡'}
                          >
                            <Users size={13} />
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              openAssetGroupModal(s.id);
                            }}
                            className={`rounded-lg p-1.5 transition-colors ${
                              storyRefFlags[s.id]
                                ? 'text-amber-400 hover:text-amber-300'
                                : 'text-gray-600 hover:bg-gray-800 hover:text-gray-300'
                            }`}
                            title={storyRefFlags[s.id] ? '默认垫图（已设定）' : '设置默认垫图'}
                          >
                            <ImagePlus size={13} />
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleExport(s);
                            }}
                            disabled={exportingStoryId === s.id}
                            className="rounded-lg p-1.5 text-gray-600 transition-colors hover:bg-gray-800 hover:text-sky-300 disabled:opacity-40"
                            title="导出整本作品"
                          >
                            {exportingStoryId === s.id ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              startEdit(s);
                            }}
                            className="rounded-lg p-1.5 text-gray-600 transition-colors hover:bg-gray-800 hover:text-gray-300"
                            title="编辑"
                          >
                            <Pencil size={13} />
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDelete(s.id);
                            }}
                            className="rounded-lg p-1.5 text-gray-600 transition-colors hover:bg-gray-800 hover:text-red-400"
                            title="删除"
                          >
                            <Trash2 size={13} />
                          </button>
                          <button
                            onClick={() => onSelectStory(s)}
                            className="flex items-center gap-1 rounded-xl bg-violet-600/20 px-3 py-1.5 text-xs font-medium text-violet-400 transition-colors hover:bg-violet-600 hover:text-white"
                          >
                            进入
                            <ChevronRight size={13} />
                          </button>
                        </div>
                      </div>
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </main>

      {/* Asset groups modal */}
      {assetModalStoryId !== null && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-3 sm:p-4"
          onClick={() => setAssetModalStoryId(null)}
        >
          <div
            className="flex max-h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-gray-700 bg-gray-900 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-gray-800 px-5 py-4">
              <div>
                <h3 className="flex items-center gap-2 text-sm font-semibold">
                  <Sparkles size={16} className="text-violet-400" />
                  全局设定组
                </h3>
                <p className="mt-1 text-xs text-gray-500">为作品管理角色外貌卡和参考图组。</p>
              </div>
              <button
                onClick={() => setAssetModalStoryId(null)}
                className="rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-800 hover:text-gray-300"
              >
                <X size={16} />
              </button>
            </div>

            {assetModalLoading ? (
              <div className="flex items-center justify-center py-20 text-gray-500">
                <Loader2 size={24} className="animate-spin" />
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-[220px_1fr] min-h-0 flex-1">
                <div className="border-b md:border-b-0 md:border-r border-gray-800 p-3 overflow-y-auto">
                  <button
                    onClick={addAssetGroup}
                    className="mb-3 flex w-full items-center justify-center gap-1.5 rounded-xl bg-violet-600 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-violet-500"
                  >
                    <Plus size={13} />
                    新增设定组
                  </button>
                  <div className="space-y-1">
                    {assetGroups.map((group) => {
                      const key = group.id === null ? 'default' : String(group.id);
                      const active = key === assetSelectedKey;
                      return (
                        <button
                          key={key}
                          onClick={() => selectAssetGroup(group)}
                          className={`w-full text-left px-3 py-2 rounded-lg border transition-colors ${
                            active
                              ? 'bg-violet-600/20 border-violet-500 text-violet-400'
                              : 'bg-gray-950/40 border-gray-800 text-gray-400 hover:text-gray-200 hover:border-gray-700'
                          }`}
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs font-medium truncate">{group.name}</span>
                            {group.is_default && <span className="text-[10px] text-blue-300 shrink-0">默认</span>}
                          </div>
                          <div className="mt-1 text-[10px] text-gray-500">
                            {group.has_character_profiles ? '角色卡' : '无角色卡'} · {group.ref_count} 张垫图
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </div>

                <div className="min-h-0 flex flex-col">
                  {activeAssetGroup ? (
                    <>
                      <div className="space-y-3 border-b border-gray-800 p-4">
                        <div className="flex items-center gap-2">
                          <input
                            value={assetDraftName}
                            onChange={(e) => setAssetDraftName(e.target.value)}
                            disabled={activeAssetGroup.is_default}
                            className="flex-1 rounded-xl border border-gray-700 bg-gray-800 px-3 py-2 text-sm focus:border-violet-500 focus:outline-none disabled:opacity-60"
                          />
                          {!activeAssetGroup.is_default && (
                            <button
                              onClick={removeAssetGroup}
                              className="rounded-xl border border-red-900 bg-red-950/40 p-2 text-red-300 transition-colors hover:bg-red-900/60"
                              title="删除设定组"
                            >
                              <Trash2 size={15} />
                            </button>
                          )}
                        </div>
                        <textarea
                          value={assetDraftChars}
                          onChange={(e) => setAssetDraftChars(e.target.value)}
                          rows={8}
                          className="w-full resize-none rounded-xl border border-gray-700 bg-gray-800 p-3 text-sm leading-relaxed text-gray-200 outline-none focus:border-violet-500"
                          placeholder={`角色名：塞蕾娜\n外貌：银灰色长发，冰蓝色眼睛...\n\n角色名：艾莉西亚\n外貌：金色长发，紫色眼睛...`}
                        />
                        <div className="flex justify-end">
                          <button
                            onClick={saveAssetGroup}
                            disabled={assetModalSaving}
                            className="rounded-xl bg-violet-600 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-violet-500 disabled:opacity-40"
                          >
                            {assetModalSaving ? '保存中…' : '保存角色卡'}
                          </button>
                        </div>
                      </div>

                      <div className="flex-1 overflow-y-auto p-4">
                        <div className="flex items-center justify-between mb-3">
                          <div className="text-xs text-gray-500">本组垫图 {activeAssetGroup.ref_count}/{refModalMax} 张</div>
                          <button
                            onClick={() => assetFileRef.current?.click()}
                            disabled={assetRefUploading || activeAssetGroup.ref_count >= refModalMax}
                            className="flex items-center gap-1.5 rounded-xl bg-violet-600 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-violet-500 disabled:opacity-30"
                          >
                            {assetRefUploading ? <Loader2 size={13} className="animate-spin" /> : <ImagePlus size={13} />}
                            添加垫图
                          </button>
                        </div>
                        {activeAssetGroup.ref_images.length === 0 ? (
                          <button
                            onClick={() => assetFileRef.current?.click()}
                            disabled={assetRefUploading}
                            className="flex w-full flex-col items-center justify-center rounded-2xl border-2 border-dashed border-gray-700 py-12 text-gray-500 transition-colors hover:border-amber-500/50 hover:text-gray-400 disabled:opacity-40"
                          >
                            <ImagePlus size={28} className="mb-2" />
                            <span className="text-sm">点击上传本组第一张垫图</span>
                          </button>
                        ) : (
                          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
                            {activeAssetGroup.ref_images.map((img) => (
                              <div key={img.filename} className="relative aspect-square overflow-hidden rounded-xl border border-gray-700 bg-gray-950">
                                <img
                                  src={mangaThumbUrl(img.image_path, 480, img.filename)!}
                                  alt={img.filename}
                                  className="w-full h-full object-cover"
                                  loading="lazy"
                                  decoding="async"
                                />
                                <div className="absolute inset-0 bg-black/0 hover:bg-black/40 transition-colors flex items-end p-2 pointer-events-none">
                                  <span className="text-[10px] text-white/80 bg-black/60 px-1.5 py-0.5 rounded">{img.size_kb} KB</span>
                                </div>
                                <button
                                  onClick={() => handleAssetRefDelete(img.filename)}
                                  className="absolute top-1.5 right-1.5 p-1 rounded-md bg-red-600 hover:bg-red-500 text-white shadow-lg transition-colors"
                                  title="删除垫图"
                                >
                                  <Trash2 size={12} />
                                </button>
                              </div>
                            ))}
                          </div>
                        )}
                        <input
                          ref={assetFileRef}
                          type="file"
                          accept="image/*"
                          className="hidden"
                          onChange={(e) => {
                            const file = e.target.files?.[0];
                            if (file) handleAssetRefUpload(file);
                            e.target.value = '';
                          }}
                        />
                      </div>
                    </>
                  ) : (
                    <div className="flex items-center justify-center py-20 text-gray-500">暂无设定组</div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Character card modal */}
      {charModalStoryId !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-3 sm:p-4">
          <div className="flex max-h-[calc(100vh-24px)] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-gray-700 bg-gray-900 shadow-2xl sm:max-h-[calc(100vh-32px)]">
            <div className="flex items-center justify-between border-b border-gray-800 px-5 py-4">
              <div>
                <h3 className="flex items-center gap-2 text-sm font-semibold">
                  <Users size={16} className="text-violet-400" />
                  全局角色外貌卡
                </h3>
                <p className="mt-1 text-xs text-gray-500">统一维护人物外貌，减少生成偏差。</p>
              </div>
              <button
                onClick={() => setCharModalStoryId(null)}
                className="rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-800 hover:text-gray-300"
              >
                <X size={16} />
              </button>
            </div>
            <div className="px-5 py-4 overflow-y-auto">
              {charModalLoading ? (
                <div className="flex items-center justify-center py-12 text-gray-500">
                  <Loader2 size={24} className="animate-spin" />
                </div>
              ) : (
                <>
                  <p className="text-xs text-gray-500 mb-3">
                    在此设定角色外貌，所有章节默认继承。章节内也可单独覆盖。
                  </p>
                  <textarea
                    value={charModalText}
                    onChange={(e) => setCharModalText(e.target.value)}
                    className="w-full resize-none rounded-xl border border-gray-700 bg-gray-800 p-3 text-sm leading-relaxed text-gray-200 outline-none focus:border-violet-500"
                    rows={10}
                    placeholder={`角色名：塞蕾娜\n性别：女\n发色与发型：银灰色长发…\n\n角色名：艾伦\n性别：男\n…`}
                    autoFocus
                  />
                </>
              )}
            </div>
            <div className="flex justify-end gap-2 border-t border-gray-800 px-5 py-3">
              <button
                onClick={() => setCharModalStoryId(null)}
                className="rounded-xl px-4 py-2 text-sm text-gray-400 transition-colors hover:bg-gray-800 hover:text-gray-200"
              >
                取消
              </button>
              <button
                onClick={saveCharModal}
                disabled={charModalSaving || charModalLoading}
                className="rounded-xl bg-violet-600 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-violet-500 disabled:opacity-40"
              >
                {charModalSaving ? '保存中…' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Ref images modal (multi) */}
      {refModalStoryId !== null && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-3 sm:p-4"
          onClick={() => setRefModalStoryId(null)}
        >
          <div
            className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-gray-700 bg-gray-900 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-gray-800 px-5 py-4">
              <div>
                <h3 className="flex items-center gap-2 text-sm font-semibold">
                  <ImagePlus size={16} className="text-amber-400" />
                  全局默认垫图
                  <span className="text-xs font-normal text-gray-500">
                    {refModalImages.length}/{refModalMax} 张
                  </span>
                </h3>
                <p className="mt-1 text-xs text-gray-500">给所有章节提供默认视觉参考。</p>
              </div>
              <button
                onClick={() => setRefModalStoryId(null)}
                className="rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-800 hover:text-gray-300"
              >
                <X size={16} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-4">
              <p className="text-xs text-gray-500 mb-4 leading-relaxed">
                上传默认垫图（最多 {refModalMax} 张），所有章节默认继承，用作人物外貌和画面参考。章节内也可单独覆盖。
              </p>
              {refModalLoading ? (
                <div className="flex items-center justify-center py-12 text-gray-500">
                  <Loader2 size={24} className="animate-spin" />
                </div>
              ) : refModalImages.length === 0 ? (
                <button
                  onClick={() => refModalFileRef.current?.click()}
                  disabled={refModalUploading}
                  className="flex w-full cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed border-gray-700 py-12 text-gray-500 transition-colors hover:border-amber-500/50 hover:text-gray-400 disabled:opacity-40"
                >
                  {refModalUploading ? (
                    <Loader2 size={28} className="animate-spin mb-2" />
                  ) : (
                    <ImagePlus size={28} className="mb-2" />
                  )}
                  <span className="text-sm">{refModalUploading ? '上传中…' : '点击上传第一张垫图'}</span>
                </button>
              ) : (
                <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                  {refModalImages.map((img) => (
                    <div
                      key={img.filename}
                      className="group relative aspect-square overflow-hidden rounded-xl border border-gray-700 bg-gray-950"
                    >
                      <img
                        src={mangaThumbUrl(img.image_path, 480, img.filename)!}
                        alt={img.filename}
                        className="w-full h-full object-cover"
                        loading="lazy"
                        decoding="async"
                      />
                      <div className="absolute inset-0 bg-black/0 group-hover:bg-black/40 transition-colors flex items-end p-2 pointer-events-none">
                        <span className="text-[10px] text-white/80 bg-black/60 px-1.5 py-0.5 rounded">
                          {img.size_kb} KB
                        </span>
                      </div>
                      <button
                        onClick={() => handleRefDelete(img.filename)}
                        className="absolute top-1.5 right-1.5 p-1 rounded-md bg-red-600 hover:bg-red-500 text-white shadow-lg transition-colors"
                        title="删除"
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <input
                ref={refModalFileRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) handleRefUpload(file);
                  e.target.value = '';
                }}
              />
            </div>
            <div className="flex items-center justify-between border-t border-gray-800 px-5 py-3">
              <button
                onClick={() => refModalFileRef.current?.click()}
                disabled={refModalUploading || refModalImages.length >= refModalMax}
                className="flex items-center gap-1.5 rounded-xl bg-violet-600 px-4 py-2 text-xs font-medium text-white transition-colors hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-30"
                title={refModalImages.length >= refModalMax ? `已达上限 ${refModalMax} 张` : '上传一张垫图'}
              >
                {refModalUploading ? <Loader2 size={13} className="animate-spin" /> : <ImagePlus size={13} />}
                {refModalImages.length === 0 ? '上传垫图' : '添加一张'}
              </button>
              <button
                onClick={() => setRefModalStoryId(null)}
                className="rounded-xl px-4 py-2 text-sm text-gray-400 transition-colors hover:bg-gray-800 hover:text-gray-200"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
