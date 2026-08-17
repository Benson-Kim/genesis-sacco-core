/**
 * Screen registry for the access-control console. Each entry:
 * { id, label, title, sub, mount(container, ctx) }.
 *
 * `mount` receives the <main> container (already cleared) and a ctx:
 *   { onSessionExpired(): void, modalHost: HTMLElement }
 *
 * Screens land one per commit (incremental push discipline).
 */

import { mountUsers } from './users.js';
import { mountAudit } from './audit.js';

/** @type {Array<{id:string,label:string,title:string,sub:string,mount:(container:HTMLElement,ctx:{onSessionExpired:()=>void,modalHost:HTMLElement})=>void}>} */
export const SCREENS = [
  {
    id: 'users',
    label: 'Users',
    title: 'Access control — Users',
    sub: 'System users administration',
    mount: mountUsers,
  },
  {
    id: 'audit',
    label: 'Audit log',
    title: 'Access control — Audit log',
    sub: 'Immutable audit trail viewer',
    mount: mountAudit,
  },
];
