# macOS 拖放集成

一个薄壳：把文件拖到 App 图标上 → 输入关键词 → 选模板 → 终端里预览并确认。

## 编译

```bash
# 1. 把 dnd.sh 放到固定位置（App 里写死了这个路径）
cp dnd.sh ~/.rnbatch-dnd.sh
chmod +x ~/.rnbatch-dnd.sh

# 2. 编译 App
mkdir -p ~/Applications
osacompile -o "$HOME/Applications/统一重命名.app" UnifiedRename.applescript
```

把 `统一重命名.app` 拖进 Dock 或 Finder 侧边栏即可。

## 说明

- 需要 `rnbatch` 在 PATH 里（`pip install -e ".[all]"`）。
- 关键词和模板靠两个系统对话框输入，真正的改名在终端里完成 ——
  **因为预览表需要滚动，弹窗装不下，也容易让人不看就点确认。**
- 这是过渡方案。正式 GUI 见 [docs/ROADMAP.md](../../docs/ROADMAP.md) 的 v0.4。
