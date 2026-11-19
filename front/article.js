// 글(문단 블록) 관련 순수 함수 — app.js와 테스트에서 함께 씀.

export function newBlockId() {
  return 'b_' + Math.random().toString(36).slice(2, 12);
}

export function copyArticle(a) {
  return { title: a?.title ?? '', title_locked: !!a?.title_locked, blocks: (a?.blocks ?? []).map(b => ({ id: b.id, text: b.text, locked: !!b.locked })) };
}

// 서버에 보낼 글: 빈 문단은 빼고 보낸다.
export function articlePayload(a) {
  return { title: a.title, title_locked: a.title_locked, blocks: a.blocks.filter(b => b.text.trim()).map(b => ({ id: b.id, text: b.text, locked: b.locked })) };
}

// AI가 새로 쓴 글(server)을 받을 때, 요청을 보낸 뒤 사용자가 화면에서 고친 부분(local)은 지킨다.
//  editedIds: 그사이 고친 문단 id,  deletedIds: 그사이 지운 문단 id,  titleEdited: 그사이 제목을 고쳤는지
export function mergeServerArticle(server, local, { editedIds = new Set(), deletedIds = new Set(), titleEdited = false } = {}) {
  const result = copyArticle(server);
  result.blocks = result.blocks.filter(b => !deletedIds.has(b.id));
  local.blocks.forEach((lb, idx) => {
    if (!editedIds.has(lb.id)) return;
    const at = result.blocks.findIndex(b => b.id === lb.id);
    if (at >= 0) { result.blocks[at] = { ...lb }; return; }
    let pos = 0;                                     // 서버에 아직 없는 새 문단: 원래 앞 문단 뒤에
    for (let j = idx - 1; j >= 0; j--) {
      const k = result.blocks.findIndex(b => b.id === local.blocks[j].id);
      if (k >= 0) { pos = k + 1; break; }
    }
    result.blocks.splice(pos, 0, { ...lb });
  });
  if (titleEdited) { result.title = local.title; result.title_locked = true; }
  return result;
}

export function charCount(a) {
  return a.title.length + a.blocks.reduce((n, b) => n + b.text.length, 0);
}
