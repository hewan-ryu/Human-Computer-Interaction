import test from 'node:test';
import assert from 'node:assert/strict';
import { articlePayload, mergeServerArticle } from './article.js';

const server = { title: 'AI 제목', title_locked: false, blocks: [{ id: 'a', text: 'AI 1', locked: false }, { id: 'b', text: 'AI 2', locked: false }] };

test('요청 중에 고친 문단과 제목은 서버 결과보다 우선한다', () => {
  const local = { title: '내 제목', title_locked: true, blocks: [{ id: 'a', text: '내가 고침', locked: true }, { id: 'n', text: '새 문단', locked: true }, { id: 'b', text: 'old', locked: false }] };
  const merged = mergeServerArticle(server, local, { editedIds: new Set(['a', 'n']), titleEdited: true });
  assert.deepEqual(merged.blocks.map(b => b.text), ['내가 고침', '새 문단', 'AI 2']);
  assert.equal(merged.title, '내 제목');
  assert.equal(merged.title_locked, true);
});

test('요청 중에 지운 문단은 다시 나타나지 않는다', () => {
  const local = { title: '', title_locked: false, blocks: [{ id: 'b', text: 'AI 2', locked: false }] };
  const merged = mergeServerArticle(server, local, { deletedIds: new Set(['a']) });
  assert.deepEqual(merged.blocks.map(b => b.id), ['b']);
  assert.equal(merged.title, 'AI 제목');
});

test('빈 문단은 서버에 보내지 않는다', () => {
  const payload = articlePayload({ title: 't', title_locked: false, blocks: [{ id: 'a', text: '  ', locked: true }, { id: 'b', text: '내용', locked: true }] });
  assert.deepEqual(payload.blocks.map(b => b.id), ['b']);
});
