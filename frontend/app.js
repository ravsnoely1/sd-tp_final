'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('prosa-token'), me, rooms = [], current = null, socket;
let registering = false, generation = 0, reconnectTimer, retries = 0, nextBefore = null;
let displayed = new Set(), unread = new Map(), pending = new Map(), historyEpoch = 0;
const title = room => room.kind === 'group' ? room.name : room.members.find(u => u.id !== me.id)?.username || 'Conversa';
const initials = text => text.slice(0, 2).toUpperCase();
function element(tag, cls, text) { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = text; return el; }
async function api(path, method = 'GET', data) {
  const response = await fetch(path, {method, headers: {'Content-Type': 'application/json', ...(token ? {Authorization: `Bearer ${token}`} : {})}, ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || 'Não foi possível concluir.'); error.status = response.status; throw error; }
  return result;
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = false; }
function status(text, online) { $('connection').textContent = text; $('status-dot').classList.toggle('online', online); $('send').disabled = !online; }
$('toggle-auth').onclick = () => { registering = !registering; $('auth-title').textContent = registering ? 'Faça parte da conversa.' : 'Bom ter você aqui.'; $('auth-description').textContent = registering ? 'Crie sua conta para começar.' : 'Entre para continuar suas conversas.'; $('auth-submit').textContent = registering ? 'Criar conta →' : 'Entrar →'; $('toggle-auth').textContent = registering ? 'Já tem uma conta? Entre' : 'Primeira vez aqui? Crie sua conta'; $('password').autocomplete = registering ? 'new-password' : 'current-password'; $('auth-error').textContent = ''; };
$('auth-form').onsubmit = async event => {
  event.preventDefault(); $('auth-submit').disabled = true; $('auth-error').textContent = '';
  try { const data = {username: $('username').value, password: $('password').value}; if (registering) await api('/api/auth/register', 'POST', data); const session = await api('/api/auth/login', 'POST', data); token = session.token; sessionStorage.setItem('prosa-token', token); $('password').value = ''; await enter(); }
  catch (error) { $('auth-error').textContent = error.message; } finally { $('auth-submit').disabled = false; }
};
async function enter() {
  me = await api('/api/auth/me'); $('login-view').hidden = true; $('chat-view').hidden = false; $('current-user').textContent = me.username; $('avatar').textContent = initials(me.username); connect();
}
function renderRooms() {
  const root = $('rooms'); root.replaceChildren();
  for (const room of rooms.filter(r => title(r).toLowerCase().includes($('search').value.toLowerCase()))) {
    const button = element('button', 'room-item' + (current?.id === room.id ? ' selected' : ''));
    button.append(element('span', 'avatar', room.kind === 'group' ? '#' : initials(title(room))));
    const info = element('span'); info.append(element('strong', '', title(room)), element('small', '', room.kind === 'group' ? `${room.members.length} participantes` : 'Conversa privada')); button.append(info);
    if (unread.get(room.id)) button.append(element('span', 'badge', String(unread.get(room.id))));
    button.onclick = () => openRoom(room); root.append(button);
  }
  if (!root.children.length) root.append(element('p', 'text-button', 'Nenhuma conversa por aqui ainda.'));
}
async function refreshRooms() { rooms = (await api('/api/chat/rooms')).rooms; renderRooms(); }
function addMessage(message, prepend = false) {
  if (current?.id !== message.room_id || displayed.has(message.id)) return;
  displayed.add(message.id); const bubble = element('article', 'bubble' + (message.sender_id === me.id ? ' mine' : ''));
  bubble.dataset.id = message.id; bubble.dataset.created = message.created_at;
  bubble.append(element('strong', '', message.sender_name), element('p', '', message.content));
  const time = element('time', '', new Date(message.created_at * 1000).toLocaleString('pt-BR', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})); time.dateTime = new Date(message.created_at * 1000).toISOString(); bubble.append(time);
  // Histórico e eventos podem chegar em ordens diferentes.
  const later = [...$('messages').children].find(node => Number(node.dataset.created) > message.created_at || (Number(node.dataset.created) === message.created_at && node.dataset.id > message.id));
  $('messages').insertBefore(bubble, later || null); if (!prepend) $('messages').scrollTop = $('messages').scrollHeight;
}
async function openRoom(room) {
  current = room; unread.delete(room.id); displayed.clear(); $('messages').replaceChildren(); $('room-title').textContent = title(room); $('room-avatar').textContent = room.kind === 'group' ? '#' : initials(title(room)); $('room-members').textContent = room.members.map(u => u.username).join(', '); $('empty-state').hidden = true; $('active-room').hidden = false; $('chat-view').classList.add('room-open'); renderRooms();
  const epoch = ++historyEpoch;
  try { const result = await api(`/api/chat/rooms/${room.id}/messages`); if (epoch !== historyEpoch) return; result.messages.forEach(m => addMessage(m)); nextBefore = result.next_before; $('older').hidden = !nextBefore; }
  catch (error) { notice(error.message); }
}
$('older').onclick = async () => {
  const roomId = current.id, epoch = historyEpoch; $('older').disabled = true;
  try { const result = await api(`/api/chat/rooms/${roomId}/messages?before=${encodeURIComponent(nextBefore)}`); if (epoch !== historyEpoch) return; const previousHeight = $('messages').scrollHeight; result.messages.forEach(m => addMessage(m, true)); nextBefore = result.next_before; $('older').hidden = !nextBefore; $('messages').scrollTop += $('messages').scrollHeight - previousHeight; } catch (error) { notice(error.message); } finally { $('older').disabled = false; }
};
function connect() {
  clearTimeout(reconnectTimer); const gen = ++generation; status('Conectando…', false);
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket.onopen = () => { if (gen === generation) socket.send(JSON.stringify({type:'auth', token})); };
  socket.onmessage = async event => {
    if (gen !== generation) return; const data = JSON.parse(event.data);
    try {
      if (data.type === 'ready') { retries = 0; status('Conectado', true); $('notice').hidden = true; await refreshRooms(); if (current) await openRoom(rooms.find(r => r.id === current.id) || current); for (const item of pending.values()) socket.send(JSON.stringify(item)); }
      if (data.type === 'room') { if (!rooms.some(r => r.id === data.room.id)) rooms.unshift(data.room); renderRooms(); }
      if (data.type === 'message' || data.type === 'ack') { const message = data.message; pending.delete(message.client_id); if (current?.id === message.room_id) addMessage(message); else if (data.type === 'message') { unread.set(message.room_id, (unread.get(message.room_id) || 0) + 1); renderRooms(); } }
      if (data.type === 'error') { if (data.client_id) { const failed = pending.get(data.client_id); if (failed && !$('message').value) $('message').value = failed.content; pending.delete(data.client_id); } if (data.status === 401) return leave(); notice(data.error); }
    } catch (error) { if (error.status === 401) leave(); else notice(error.message); }
  };
  socket.onclose = () => { if (gen !== generation || !token) return; status('Reconectando…', false); reconnectTimer = setTimeout(connect, Math.min(1000 * 2 ** retries++, 10000) + Math.random() * 500); };
  socket.onerror = () => socket.close();
}
$('message-form').onsubmit = event => { event.preventDefault(); const content = $('message').value.trim(); if (!content || !current || socket?.readyState !== WebSocket.OPEN) return; const data = {type:'send', room_id:current.id, content, client_id:crypto.randomUUID()}; pending.set(data.client_id, data); socket.send(JSON.stringify(data)); $('message').value = ''; };
$('message').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('message-form').requestSubmit(); } };
function leave() { ++generation; ++historyEpoch; clearTimeout(reconnectTimer); socket?.close(); token = null; sessionStorage.removeItem('prosa-token'); current = null; rooms = []; pending.clear(); unread.clear(); $('chat-view').hidden = true; $('login-view').hidden = false; $('active-room').hidden = true; $('empty-state').hidden = false; $('chat-view').classList.remove('room-open'); $('auth-error').textContent = ''; }
$('logout').onclick = async () => { try { await api('/api/auth/logout', 'POST', {}); leave(); } catch (error) { notice(error.message); } };
$('search').oninput = renderRooms;
$('back').onclick = () => $('chat-view').classList.remove('room-open');
async function newRoom() {
  $('room-error').textContent = ''; $('people').replaceChildren(element('legend', '', 'Participantes'));
  try { const {users} = await api('/api/auth/users'); for (const user of users.filter(u => u.id !== me.id)) { const label = element('label'); const input = element('input'); input.type = 'checkbox'; input.value = user.id; label.append(input, document.createTextNode(user.username)); $('people').append(label); } if (users.length < 2) $('people').append(element('p', '', 'Convide alguém para criar uma conta.')); $('room-dialog').showModal(); } catch (error) { notice(error.message); }
}
$('new-room').onclick = newRoom; $('empty-new').onclick = newRoom; $('close-dialog').onclick = () => $('room-dialog').close();
$('room-kind').onchange = () => { $('group-name-wrap').hidden = $('room-kind').value !== 'group'; $('group-name').required = $('room-kind').value === 'group'; };
$('room-form').onsubmit = async event => { event.preventDefault(); const button = event.submitter; button.disabled = true; try { const members = [...$('people').querySelectorAll('input:checked')].map(i => i.value); const kind = $('room-kind').value; if (kind === 'direct' && members.length !== 1) throw new Error('Selecione uma pessoa para a conversa privada.'); const room = await api('/api/chat/rooms', 'POST', {kind, name:$('group-name').value, members}); await refreshRooms(); $('room-dialog').close(); await openRoom(room); } catch (error) { $('room-error').textContent = error.message; } finally { button.disabled = false; } };
if (token) enter().catch(error => { leave(); $('auth-error').textContent = error.message; });
