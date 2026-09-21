/**
 * mineflayer bridge — HTTP API + 3D viewer for Minecraft bot.
 *
 * Usage:
 *   node bridge_server.js
 *
 * Env vars: MC_HOST, MC_PORT, BOT_NAME, MC_VERSION, BRIDGE_PORT, VIEWER_PORT
 *
 * Endpoints:
 *   GET  /health           → bot status
 *   GET  /state            → bot position, nearby blocks, entities, chat, inventory_items
 *   POST /action           → execute one action
 *   GET  /phase            → benchmark phase + counters
 *   POST /phase            → set benchmark phase (optionally reset counters)
 *   POST /benchmark/reset  → run a full tech-tree benchmark world setup
 * 
 * 3D viewer on port 3007 (prismarine-viewer).
 *
 * Usage:
 *   $env:MC_HOST="localhost"; $env:MC_PORT="25565"; node bridge_server.js
 */

const express = require('express');
const mineflayer = require('mineflayer');
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder');
const collectBlock = require('mineflayer-collectblock');
const Vec3 = require('vec3'); // bot.blockAt() needs a Vec3; plain {x,y,z} throws "pos.floored is not a function"
const app = express();
app.use(express.json());

const HOST = process.env.MC_HOST || 'localhost';
const PORT = parseInt(process.env.MC_PORT || '25565', 10);
const BOT_NAME = process.env.BOT_NAME || 'paos';
const API_PORT = parseInt(process.env.BRIDGE_PORT || '3001', 10);
const VIEWER_PORT = parseInt(process.env.VIEWER_PORT || '3007', 10);
const STATE_RADIUS = parseInt(process.env.STATE_RADIUS || '5', 10);
const ACTION_TYPES = [
    'move', 'look', 'jump', 'sneak', 'sprint', 'attack', 'interact',
    'place', 'dig', 'use', 'select_slot', 'drop', 'chat', 'collect',
    'equip', 'craft', 'smelt'
];

let bot = null, botSpawned = false, spawnTime = 0;
let viewerStarted = false; // prismarine-viewer binds a port once; spawn fires again on respawn
let recentChats = []; // recent chat messages from other players, exposed via /state last_chats
let activeDig = null; // prevent overlapping digs from aborting each other

// ── Benchmark phase tracking ────────────────────────────────────
// Mirror of PhyAgentOS/benchmarks/minecraft/techtree phase semantics.
// The Python adapter posts /phase before/after a benchmark reset so the
// bridge knows the bot is in a benchmark-driven episode.
let currentPhase = 'idle';
let phaseCounters = { resets: 0, steps: 0 };

// ── Bot ─────────────────────────────────────────────────────────
const MC_VERSION = process.env.MC_VERSION || '1.20.4';

function createBot() {
    bot = mineflayer.createBot({ host: HOST, port: PORT, username: BOT_NAME, version: MC_VERSION });
    bot.loadPlugin(pathfinder);
    bot.loadPlugin(collectBlock.plugin);

    bot.on('spawn', () => {
        botSpawned = true; spawnTime = Date.now();
        console.log(`[bridge] Bot spawned: ${BOT_NAME} (MC ${MC_VERSION})`);

        // mineflayer-collectblock init
        if (bot.collectBlock) {
            if (!bot.collectBlock.chestLocations) bot.collectBlock.chestLocations = new Map();
            if (!bot.collectBlock.chestsToOpen) bot.collectBlock.chestsToOpen = [];
            if (!bot.collectBlock.tempChests) bot.collectBlock.tempChests = new Map();
        }

        if (!viewerStarted) {
            try {
                const { mineflayer: mineflayerViewer } = require('prismarine-viewer');
                mineflayerViewer(bot, { port: VIEWER_PORT, firstPerson: true });
                viewerStarted = true;
                console.log(`[bridge] 3D viewer (first-person) on http://localhost:${VIEWER_PORT}`);
            } catch (e) { console.log(`[bridge] 3D viewer unavailable: ${e.message}`); }
        }
    });

    bot.on('death', () => { botSpawned = false; setTimeout(() => { if (bot) bot.respawn(); }, 3000); });
    bot.on('kicked', (r) => { console.log(`[bridge] Kicked: ${r}`); botSpawned = false; });
    bot.on('error', (e) => console.error(`[bridge] Error: ${e.message}`));
    bot.on('end', (r) => { console.log(`[bridge] Disconnected: ${r}`); botSpawned = false; });

    bot.on('chat', (username, message) => {
        if (username === BOT_NAME) return;
        recentChats.push({ username, message, time: Date.now() });
    });
}

// ── State ───────────────────────────────────────────────────────
function getState() {
    if (!bot || !bot.entity) return { bot: null, error: 'not spawned' };

    const pos = bot.entity.position;
    const nearbyBlocks = [];
    const nearbyEntities = [];

    // nearby blocks (radius = STATE_RADIUS)
    const r = STATE_RADIUS;
    for (let dx = -r; dx <= r; dx++) {
        for (let dy = -r; dy <= r; dy++) {
            for (let dz = -r; dz <= r; dz++) {
                const b = bot.blockAt(pos.offset(dx, dy, dz));
                if (b && b.name !== 'air') {
                    nearbyBlocks.push({
                        name: b.name,
                        position: { x: pos.x + dx, y: pos.y + dy, z: pos.z + dz }
                    });
                }
            }
        }
    }

    // nearby entities
    for (const id in bot.entities) {
        const e = bot.entities[id];
        if (e === bot.entity) continue;
        const dist = e.position.distanceTo(pos);
        if (dist <= STATE_RADIUS * 2) {
            nearbyEntities.push({
                type: e.name || e.type || 'unknown',
                position: { x: e.position.x, y: e.position.y, z: e.position.z },
                health: e.health,
            });
        }
    }

    // players
    const players = Object.values(bot.players).map(p => ({
        username: p.username,
        position: {
            x: Math.round(p.entity.position.x * 10) / 10,
            y: Math.round(p.entity.position.y * 10) / 10,
            z: Math.round(p.entity.position.z * 10) / 10,
        },
    }));

    // inventory hotbar
    const hotbarSlots = bot.inventory.slots.slice(36, 45);
    const hotbar = hotbarSlots.map((item, i) => item ? {
        slot: i,
        name: item.name,
        count: item.count,
    } : null).filter(Boolean);

    // full inventory flattened into the evaluator-friendly shape:
    //   [{ name: "minecraft:oak_log", count: 1 }, ...]
    // so PhyAgentOS.benchmarks.minecraft.techtree.evaluator.inventory_counts
    // can score the bridge state directly without a second adapter layer.
    const inventory_items = bot.inventory.items().map((item) => ({
        name: mcName(item.name),
        count: item.count,
    }));

    return {
        bot: {
            position: { x: pos.x, y: pos.y, z: pos.z },
            rotation: { yaw: bot.entity.yaw, pitch: bot.entity.pitch },
            on_ground: bot.entity.onGround,
            health: bot.health,
        },
        health: bot.health,
        hunger: bot.food,
        dimension: bot.game.dimension,
        world: { time: bot.time.timeOfDay, raining: bot.isRaining },
        player_list: Object.keys(bot.players),
        nearby_blocks: nearbyBlocks,
        nearby_entities: nearbyEntities,
        players: players,
        inventory: { hotbar },
        inventory_items,
        last_chats: recentChats,
    };
}

function clearActiveDig() {
    activeDig = null;
}

function eyeToBlockDistance(block) {
    if (!bot?.entity || !block?.position) return Infinity;
    return block.position.offset(0.5, 0.5, 0.5).distanceTo(bot.entity.position.offset(0, bot.entity.eyeHeight, 0));
}

function isPlacementCollidingWithBot(placeTarget) {
    if (!bot?.entity || !placeTarget) return false;
    const feet = bot.entity.position.floored();
    const head = feet.offset(0, 1, 0);
    return placeTarget.equals(feet) || placeTarget.equals(head);
}

async function digBlockWithTimeout(block, timeoutMs) {
    const digPromise = bot.dig(block, true);
    const timeoutPromise = new Promise((_, reject) => {
        setTimeout(() => reject(new Error(`dig timeout after ${timeoutMs}ms`)), timeoutMs);
    });
    try {
        await Promise.race([digPromise, timeoutPromise]);
    } catch (e) {
        if (String(e?.message || e).includes('dig timeout')) {
            try { bot.stopDigging(); } catch (_) {}
        }
        throw e;
    }
}

// ── Actions ─────────────────────────────────────────────────────
function executeAction(action) {
    return new Promise((resolve) => {
        if (!bot || !bot.entity) return resolve({ ok: false, result: 'bot not spawned' });
        const t = action.type, p = action.params || {};
        try {
            switch (t) {
                case 'move': {
                    const pos = bot.entity.position;
                    let gx, gy, gz;
                    if (p.forward != null) {
                        const dist = parseFloat(p.forward);
                        gx = pos.x - Math.sin(bot.entity.yaw) * dist;
                        gy = pos.y;
                        gz = pos.z + Math.cos(bot.entity.yaw) * dist;
                    } else {
                        gx = p.absolute ? parseFloat(p.dx) : pos.x + parseFloat(p.dx || 0);
                        gy = p.absolute ? parseFloat(p.dy) : pos.y + parseFloat(p.dy || 0);
                        gz = p.absolute ? parseFloat(p.dz) : pos.z + parseFloat(p.dz || 0);
                    }
                    bot.pathfinder.setMovements(new Movements(bot));
                    bot.pathfinder.setGoal(new goals.GoalBlock(Math.floor(gx), Math.floor(gy), Math.floor(gz)));
                    resolve({ ok: true, result: `moving to (${gx.toFixed(1)}, ${gy.toFixed(1)}, ${gz.toFixed(1)})` }); break;
                }
                case 'look': {
                    const yaw = p.yaw != null ? parseFloat(p.yaw) * Math.PI / 180 : bot.entity.yaw;
                    const pitch = p.pitch != null ? parseFloat(p.pitch) * Math.PI / 180 : bot.entity.pitch;
                    (async () => {
                        try {
                            await bot.look(yaw, pitch, true);
                            resolve({ ok: true, result: 'ok' });
                        } catch (e) {
                            resolve({ ok: false, result: e?.message || String(e) });
                        }
                    })();
                    break;
                }
                case 'jump': bot.setControlState('jump', true); setTimeout(() => bot.setControlState('jump', false), parseInt(p.duration_ms || 500)); resolve({ ok: true, result: 'ok' }); break;
                case 'sneak': bot.setControlState('sneak', p.start !== false); resolve({ ok: true, result: 'ok' }); break;
                case 'sprint': bot.setControlState('sprint', p.start !== false); resolve({ ok: true, result: 'ok' }); break;
                case 'dig': {
                    const Vec3 = bot.entity.position.constructor;
                    const timeoutMs = Math.max(500, parseInt(p.timeout_ms || 8000, 10));
                    const b = bot.blockAt(new Vec3(Math.floor(p.x), Math.floor(p.y), Math.floor(p.z)));
                    if (!b || b.name === 'air') return resolve({ ok: false, result: 'no block' });
                    if (activeDig) {
                        const sameTarget = activeDig.position?.equals?.(b.position);
                        return resolve({ ok: false, result: sameTarget ? 'dig already in progress for target' : 'another dig already in progress' });
                    }
                    if (!b.diggable) return resolve({ ok: false, result: `${b.name} is not diggable` });
                    const distance = eyeToBlockDistance(b);
                    if (distance > 5.1 || !bot.canDigBlock(b)) {
                        return resolve({ ok: false, result: `dig failed: too far (${distance.toFixed(2)} > 5.10)` });
                    }
                    activeDig = { position: b.position.clone(), startedAt: Date.now() };
                    (async () => {
                        try {
                            await digBlockWithTimeout(b, timeoutMs);
                            resolve({ ok: true, result: `dug ${b.name}` });
                        } catch (e) {
                            const message = e?.message || String(e);
                            if (message === 'Digging aborted') {
                                resolve({ ok: false, result: 'dig aborted' });
                            } else {
                                resolve({ ok: false, result: message });
                            }
                        } finally {
                            clearActiveDig();
                        }
                    })();
                    break;
                }
                case 'place': {
                    const Vec3 = bot.entity.position.constructor;
                    const rb = bot.blockAt(new Vec3(Math.floor(p.x), Math.floor(p.y), Math.floor(p.z)));
                    if (!rb) return resolve({ ok: false, result: 'no reference block' });
                    const fv = [{ x: 0, y: -1, z: 0 }, { x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: -1 }, { x: 0, y: 0, z: 1 }, { x: -1, y: 0, z: 0 }, { x: 1, y: 0, z: 0 }];
                    const faceIndex = parseInt(p.face) || 1;
                    const placeTarget = rb.position.offset(fv[faceIndex].x, fv[faceIndex].y, fv[faceIndex].z);
                    const targetBlock = bot.blockAt(placeTarget);
                    if (isPlacementCollidingWithBot(placeTarget)) {
                        return resolve({
                            ok: false,
                            result: 'place failed: collision with bot',
                            placed_at: { x: placeTarget.x, y: placeTarget.y, z: placeTarget.z },
                            reference_block: { x: rb.position.x, y: rb.position.y, z: rb.position.z },
                            face: faceIndex
                        });
                    }
                    if (targetBlock && targetBlock.name !== 'air') {
                        return resolve({
                            ok: false,
                            result: `place failed: target occupied by ${targetBlock.name}`,
                            placed_at: { x: placeTarget.x, y: placeTarget.y, z: placeTarget.z },
                            reference_block: { x: rb.position.x, y: rb.position.y, z: rb.position.z },
                            face: faceIndex
                        });
                    }
                    (async () => {
                        try {
                            await bot.placeBlock(rb, fv[faceIndex]);
                            resolve({
                                ok: true,
                                result: 'placed',
                                placed_at: { x: placeTarget.x, y: placeTarget.y, z: placeTarget.z },
                                reference_block: { x: rb.position.x, y: rb.position.y, z: rb.position.z },
                                face: faceIndex
                            });
                        } catch (e) {
                            resolve({
                                ok: false,
                                result: e?.message || String(e),
                                placed_at: { x: placeTarget.x, y: placeTarget.y, z: placeTarget.z },
                                reference_block: { x: rb.position.x, y: rb.position.y, z: rb.position.z },
                                face: faceIndex
                            });
                        }
                    })();
                    break;
                }
                case 'attack': {
                    let target = p.entity_id ? bot.entities[p.entity_id] : null;
                    if (!target && p.target_type) for (const id in bot.entities) if (bot.entities[id] !== bot.entity && bot.entities[id].name === p.target_type) { target = bot.entities[id]; break; }
                    if (!target) return resolve({ ok: false, result: 'no target' });
                    bot.attack(target); resolve({ ok: true, result: 'attacked' }); break;
                }
                case 'interact': {
                    const e = bot.entities[p.entity_id];
                    if (!e) return resolve({ ok: false, result: 'entity not found' });
                    (async () => {
                        try {
                            await bot.activateEntity(e);
                            resolve({ ok: true, result: 'ok' });
                        } catch (e2) {
                            resolve({ ok: false, result: e2?.message || String(e2) });
                        }
                    })();
                    break;
                }
                case 'use': bot.activateItem(); resolve({ ok: true, result: 'ok' }); break;
                case 'select_slot': { const s = Math.max(0, Math.min(8, parseInt(p.slot || 0))); bot.setQuickBarSlot(s); resolve({ ok: true, result: `slot ${s}` }); break; }
                case 'drop': {
                    const it = p.slot != null ? bot.inventory.slots[parseInt(p.slot)] : bot.inventory.slots[bot.quickBarSlot];
                    if (!it) return resolve({ ok: false, result: 'nothing to drop' });
                    (async () => {
                        try {
                            await bot.tossStack(it);
                            resolve({ ok: true, result: `dropped ${it.name}` });
                        } catch (e) {
                            resolve({ ok: false, result: e?.message || String(e) });
                        }
                    })();
                    break;
                }
                case 'chat': { const m = String(p.message || ''); if (!m) return resolve({ ok: false, result: 'empty' }); bot.chat(m); resolve({ ok: true, result: `sent: ${m}` }); break; }
                case 'collect': {
                    const mcData = require('minecraft-data')(bot.version);
                    const blockDef = mcData.blocksByName[p.block_type];
                    if (!blockDef) return resolve({ ok: false, result: `unknown or unsupported block_type: ${p.block_type}` });
                    const requestedCount = Math.max(1, parseInt(p.count || 1, 10));
                    const found = bot.findBlocks({
                        matching: blockDef.id,
                        maxDistance: parseInt(p.max_distance || 64, 10),
                        count: requestedCount
                    });
                    if (!found.length) return resolve({ ok: false, result: `no matching block found: ${p.block_type}` });
                    const targets = found
                        .map((pos) => bot.blockAt(pos))
                        .filter((block) => block && block.name !== 'air');
                    if (!targets.length) return resolve({ ok: false, result: `no collectable target found: ${p.block_type}` });
                    console.log(`[bridge] collect: ${p.block_type} x${requestedCount} (targets=${targets.length})`);
                    (async () => {
                        try {
                            await bot.collectBlock.collect(targets.slice(0, requestedCount));
                            console.log(`[bridge] collect done: ${requestedCount}x ${p.block_type}`);
                            resolve({ ok: true, result: `collected ${targets.slice(0, requestedCount).length}x ${p.block_type}` });
                        } catch (e2) {
                            console.log(`[bridge] collect failed: ${e2.message}`);
                            resolve({ ok: false, result: e2?.message || String(e2) });
                        }
                    })();
                    break;
                }
                case 'equip': {
                    const item = bot.inventory.items().find(i => i.name === p.item);
                    if (!item) return resolve({ ok: false, result: `no ${p.item}` });
                    (async () => {
                        try {
                            await bot.equip(item, p.destination || 'hand');
                            resolve({ ok: true, result: 'ok' });
                        } catch (e) {
                            resolve({ ok: false, result: e?.message || String(e) });
                        }
                    })();
                    break;
                }
                case 'craft': {
                    const mcData = require('minecraft-data')(bot.version);
                    const id = mcData.itemsByName[p.recipe_id]; if (!id) return resolve({ ok: false, result: `unknown: ${p.recipe_id}` });
                    let craftingTable = null;
                    let recipes = bot.recipesFor(id.id, null, 1, null);
                    if (!recipes.length) {
                        const tableId = mcData.blocksByName.crafting_table?.id;
                        if (tableId != null) {
                            const tablePos = bot.findBlock({ matching: tableId, maxDistance: parseInt(p.max_distance || 8, 10) });
                            if (tablePos) {
                                craftingTable = tablePos;
                                recipes = bot.recipesFor(id.id, null, 1, craftingTable);
                            }
                        }
                    }
                    if (!recipes.length) return resolve({ ok: false, result: 'no recipe' });
                    if (recipes[0].requiresTable && !craftingTable) return resolve({ ok: false, result: 'recipe requires crafting table' });
                    (async () => {
                        try {
                            await bot.craft(recipes[0], parseInt(p.count || 1, 10), craftingTable);
                            resolve({ ok: true, result: `crafted ${p.count}x ${p.recipe_id}` });
                        } catch (e) {
                            resolve({ ok: false, result: e?.message || String(e) });
                        }
                    })();
                    break;
                }
                case 'smelt': {
                    const input = bot.inventory.items().find(i => i.name === p.input);
                    const fuel = bot.inventory.items().find(i => i.name === (p.fuel || 'coal'));
                    const furnaceBlock = bot.findBlock({
                        matching: require('minecraft-data')(bot.version).blocksByName.furnace.id,
                        maxDistance: parseInt(p.max_distance || 8, 10)
                    });
                    if (!input) return resolve({ ok: false, result: `no ${p.input}` });
                    if (!fuel) return resolve({ ok: false, result: `no ${p.fuel || 'coal'}` });
                    if (!furnaceBlock) return resolve({ ok: false, result: 'no nearby furnace' });
                    (async () => {
                        let furnace = null;
                        try {
                            const count = Math.max(1, parseInt(p.count || 1, 10));
                            furnace = await bot.openFurnace(furnaceBlock);
                            await furnace.putInput(input.type, null, count);
                            await furnace.putFuel(fuel.type, null, 1);
                            const deadline = Date.now() + Math.max(15000, parseInt(p.timeout_ms || 30000, 10));
                            while ((!furnace.outputItem() || furnace.outputItem().count < count) && Date.now() < deadline) {
                                await new Promise(r => setTimeout(r, 500));
                            }
                            const output = furnace.outputItem();
                            if (!output || output.count < count) throw new Error('smelt timeout');
                            const taken = await furnace.takeOutput();
                            resolve({ ok: true, result: `smelted ${taken.count}x ${taken.name}` });
                        } catch (e) {
                            resolve({ ok: false, result: e?.message || String(e) });
                        } finally {
                            try { furnace?.close(); } catch (_) {}
                        }
                    })();
                    break;
                }
                default: resolve({ ok: false, result: `unknown type: ${t}` });
            }
        } catch (e) { resolve({ ok: false, result: e.message }); }
    });
}

// ── Benchmark setup ─────────────────────────────────────────────
// Worlds an executor-independent Minecraft tech-tree benchmark task:
// arena isolation, inventory reset, item grants (with enchantment NBT),
// and relative block placement. Mirrors the semantics of
// PhyAgentOS/benchmarks/minecraft/techtree WorldSetup so the Python
// adapter can delegate the whole reset to one POST /benchmark/reset.

const DEFAULT_ARENA = {
    enabled: true,
    origin: [-2000, 80, -2000],
    clear_radius: 8,
    clear_height: 6,
    floor_block: 'smooth_stone',
    boundary_block: 'stone_bricks',
};

function mcName(n) {
    return String(n).startsWith('minecraft:') ? String(n) : `minecraft:${n}`;
}

// Build a /give command, including enchantment NBT when requested.
//   item = { item, count, enchantments: [{ id, level }] }
function giveCmd(item) {
    let suffix = '';
    const enchs = Array.isArray(item.enchantments) ? item.enchantments : [];
    if (enchs.length) {
        const entries = enchs.map((en) => {
            const id = String(en.id || '').replace('minecraft:', '');
            const lvl = parseInt(en.level || 1, 10);
            return `{id:"minecraft:${id}",lvl:${lvl}}`;
        }).join(',');
        suffix = `{Enchantments:[${entries}]}`;
    }
    const count = Math.max(1, parseInt(item.count || 1, 10));
    return `/give @s ${mcName(item.item)}${suffix} ${count}`;
}

// Send a Minecraft server command via the bot's chat channel and wait
// briefly for the server to apply it. Server commands (/tp /fill /give
// /setblock /clear) are asynchronous, so a short delay between steps
// keeps multi-step resets from racing (e.g. /tp before /fill).
const COMMAND_SETTLE_MS = 150;
function cmd(c) {
    return new Promise((resolve) => {
        bot.chat(c);
        setTimeout(() => resolve({ ok: true, cmd: c }), COMMAND_SETTLE_MS);
    });
}

async function benchmarkReset(setup) {
    if (!bot || !bot.entity) throw new Error('bot not spawned');
    const arena = { ...DEFAULT_ARENA, ...(setup.arena || {}) };
    const origin = arena.enabled
        ? arena.origin.map((v) => parseInt(v, 10))
        : [
            Math.floor(bot.entity.position.x),
            Math.floor(bot.entity.position.y),
            Math.floor(bot.entity.position.z),
        ];
    const [x, y, z] = origin;
    const seq = [];

    if (arena.enabled === true || arena.enabled === undefined) {
        const R = Math.max(1, parseInt(arena.clear_radius, 10));
        const H = Math.max(1, parseInt(arena.clear_height, 10));
        const fy = y - 1;
        // Keep the bot alive while a possibly hostile destination chunk is
        // loaded and replaced by the isolated arena.
        seq.push('/gamemode creative @s');
        seq.push(`/tp @s ${x} ${y} ${z} 0 0`);
        seq.push(`/fill ${x - R} ${y} ${z - R} ${x + R} ${y + H} ${z + R} air`);
        seq.push(`/fill ${x - R} ${fy} ${z - R} ${x + R} ${fy} ${z + R} ${mcName(arena.floor_block)}`);
        const b = mcName(arena.boundary_block);
        seq.push(`/fill ${x - R} ${fy} ${z - R} ${x + R} ${fy} ${z - R} ${b}`);
        seq.push(`/fill ${x - R} ${fy} ${z + R} ${x + R} ${fy} ${z + R} ${b}`);
        seq.push(`/fill ${x - R} ${fy} ${z - R} ${x - R} ${fy} ${z + R} ${b}`);
        seq.push(`/fill ${x + R} ${fy} ${z - R} ${x + R} ${fy} ${z + R} ${b}`);
        // The first teleport loads the destination chunks; return to the
        // origin after the floor exists so client-side physics cannot leave
        // the bot below the arena during setup.
        seq.push(`/tp @s ${x} ${y} ${z} 0 0`);
    }
    if (setup.clear_inventory !== false) seq.push('/clear @s');
    for (const it of (setup.inventory || [])) seq.push(giveCmd(it));
    for (const blk of (setup.blocks || [])) {
        const rel = blk.relative || [0, 0, 0];
        seq.push(`/setblock ${x + parseInt(rel[0], 10)} ${y + parseInt(rel[1], 10)} ${z + parseInt(rel[2], 10)} ${mcName(blk.block)}`);
    }
    seq.push('/gamemode survival @s');
    seq.push('/effect give @s minecraft:instant_health 1 10 true');
    seq.push('/effect give @s minecraft:saturation 1 10 true');

    currentPhase = 'reset';
    phaseCounters = { resets: phaseCounters.resets + 1, steps: 0 };
    for (const c of seq) await cmd(c);
    currentPhase = 'idle';
    return { ok: true, commands: seq.length, phase: currentPhase, counters: phaseCounters };
}

// ── HTTP ────────────────────────────────────────────────────────
app.get('/health', (_req, res) => res.json({ ok: true, bot_spawned: botSpawned, actions: ACTION_TYPES, uptime_seconds: botSpawned ? Math.floor((Date.now() - spawnTime) / 1000) : 0 }));
app.get('/state', (_req, res) => res.json(getState()));
app.post('/action', async (req, res) => {
    if (!req.body?.type) return res.status(400).json({ ok: false, error: 'missing type' });
    res.json(await executeAction(req.body));
});

// Benchmark phase marker: lets an external benchmark announce that the
// bot is entering/leaving a benchmark-driven episode, optionally
// resetting the step counter. Idempotent and safe to call anytime.
app.post('/phase', (req, res) => {
    const { phase, reset_counters, source } = req.body || {};
    currentPhase = phase || 'idle';
    if (reset_counters) phaseCounters = { resets: phaseCounters.resets + 1, steps: 0 };
    console.log(`[bridge] phase=${currentPhase} source=${source || '-'}`);
    res.json({ ok: true, phase: currentPhase, counters: phaseCounters });
});

// Execute a full tech-tree benchmark reset in one call. The body is a
// WorldSetup dict (arena, clear_inventory, inventory, blocks). Returns
// the number of server commands issued and the final phase.
app.post('/benchmark/reset', async (req, res) => {
    try {
        res.json(await benchmarkReset(req.body || {}));
    } catch (e) {
        console.log(`[bridge] benchmark/reset failed: ${e.message}`);
        res.status(500).json({ ok: false, error: e.message });
    }
});

// Expose benchmark phase so a benchmark client can confirm the bridge
// has settled into idle after a reset.
app.get('/phase', (_req, res) => res.json({ ok: true, phase: currentPhase, counters: phaseCounters }));

// ── Start ───────────────────────────────────────────────────────
console.log(`[bridge] Starting for Minecraft ${MC_VERSION}`);
createBot();
app.listen(API_PORT, '0.0.0.0', () => console.log(`[bridge] HTTP API listening on port ${API_PORT}`));
