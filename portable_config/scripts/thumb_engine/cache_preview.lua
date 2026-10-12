-- Network previews decode a small excerpt of the parent's demuxer cache.
-- The decoder receives only a local filename, never the original URL.
local mp = require 'mp'
local utils = require 'mp.utils'
local M = {}

local function finite(value)
	return type(value) == 'number' and value == value and math.abs(value) < math.huge
end

function M.range_at(cache, time)
	if not finite(time) or type(cache) ~= 'table' then return nil end
	local ranges = cache['seekable-ranges']
	if type(ranges) ~= 'table' then return nil end
	for _, range in ipairs(ranges) do
		if type(range) == 'table' then
			local first, last = range.start, range['end']
			if finite(first) and finite(last) and first <= time and time < last then
				return first, last
			end
		end
	end
end

function M.new(options, decoder, on_frame, on_clear)
	local self = {generation = 0, serial = 0, active = nil, pending = nil,
		frames = {}, order = {}, target = nil, paths = {}}

	local function status(value, time)
		mp.set_property_native('user-data/thumb_engine/cache-preview', {
			status = value, time = time, cache_only = true, source = 'demuxer-cache',
		})
	end

	local function target_time(time)
		-- uosc and the thumbfast API pass time relative to the stream's start.
		if mp.get_property_native('rebase-start-time') == false then
			return time + mp.get_property_number('demuxer-start-time', 0)
		end
		return time
	end

	local function cached(time)
		return M.range_at(mp.get_property_native('demuxer-cache-state'), target_time(time))
	end

	local function cleanup(job)
		for _, path in ipairs(job.paths) do
			if os.remove(path) or not utils.file_info(path) then self.paths[path] = nil end
		end
	end

	local function valid(job)
		return job.generation == self.generation and self.target == job.key and cached(job.time) ~= nil
	end

	local function remember(job, data)
		if not self.frames[job.key] then self.order[#self.order + 1] = job.key end
		self.frames[job.key] = {data = data, w = job.w, h = job.h, time = job.time}
		while #self.order > math.max(1, math.min(options.cache_max or 64, 128)) do
			self.frames[table.remove(self.order, 1)] = nil
		end
	end

	local start_job
	local function finish(job, data)
		local own = self.active == job
		if own then self.active = nil end
		if valid(job) then
			if data then
				remember(job, data)
				on_frame(data, job.w, job.h)
				status('ready', job.time)
			else
				on_clear()
				status('unavailable', job.time)
			end
		end
		cleanup(job)
		if not own then return end
		local next_job = self.pending
		self.pending = nil
		if next_job and valid(next_job) then start_job(next_job) end
	end

	local function decode(job, excerpt)
		if not valid(job) then finish(job); return end
		local args = {
			decoder(), '--no-config', '--load-scripts=no', '--ytdl=no', '--terminal=yes',
			'--msg-level=all=error', '--audio=no', '--sub=no', '--hwdec=no', '--cache=no',
			'--idle=no', '--keep-open=no', '--force-window=no', '--frames=1', '--start=' .. job.seek,
			'--vd-lavc-threads=' .. tostring(options.sw_threads or 2),
			'--vf=scale=w=' .. job.w .. ':h=' .. job.h .. ',format=fmt=bgra',
			'--ovc=rawvideo', '--of=image2', '--ofopts=update=1', '--o=' .. job.raw,
			'--', excerpt,
		}
		job.command = mp.command_native_async({name = 'subprocess', args = args,
			playback_only = true, capture_stdout = true, capture_stderr = true, capture_size = 4096}, function(success, result)
			local data
			local frame_info = utils.file_info(job.raw)
			mp.msg.verbose('Cached preview decoder: status=' .. tostring(result and result.status)
				.. ' bytes=' .. tostring(frame_info and frame_info.size))
			if result and result.status ~= 0 then mp.msg.verbose(result.stdout or '', result.stderr or '') end
			if success and result and result.status == 0 and valid(job) then
				local file = io.open(job.raw, 'rb')
				if file then data = file:read('*a'); file:close() end
				if not data or #data ~= job.w * job.h * 4 then data = nil end
			end
			finish(job, data)
		end)
	end

	local function dump(job, index)
		if not valid(job) then finish(job); return end
		local first, last = cached(job.time)
		local time = target_time(job.time)
		-- A keyframe near the requested time is enough for a seek preview. Keep
		-- each dump small; full-range exports can stall the playback demuxer.
		local beginning = math.max(first, time - 2)
		local ending = math.min(last, time + 2)
		if ending <= beginning then finish(job); return end
		job.seek = math.max(0, time - beginning)
		local excerpt = job.paths[index]
		job.command = mp.command_native_async({'dump-cache', beginning, ending, excerpt}, function(success)
			local info = utils.file_info(excerpt)
			if not valid(job) then finish(job); return end
			if success and info and info.size > 0 then
				decode(job, excerpt)
			elseif index < 2 then
				dump(job, index + 1)
			else
				finish(job)
			end
		end)
	end

	start_job = function(job)
		self.serial = self.serial + 1
		local prefix = options.tnpath .. '.cache-' .. self.serial
		job.raw = prefix .. '.bgra'
		-- The bundled reduced FFmpeg supports MOV; ordinary builds may also
		-- provide Matroska for codecs which cannot be put in a MOV container.
		job.paths = {prefix .. '.mov', prefix .. '.mkv', job.raw}
		for _, path in ipairs(job.paths) do self.paths[path] = true end
		self.active = job
		dump(job, 1)
	end

	function self:clear()
		self.generation = self.generation + 1
		self.target, self.pending = nil, nil
		local active = self.active
		self.active = nil
		if active and active.command then mp.abort_async_command(active.command) end
		on_clear()
		status('idle')
	end

	function self:reset()
		self:clear()
		self.frames, self.order = {}, {}
		for path in pairs(self.paths) do
			if os.remove(path) or not utils.file_info(path) then self.paths[path] = nil end
		end
	end

	function self:update_cache()
		local target_evicted = false
		for key, frame in pairs(self.frames) do
			if not cached(frame.time) then
				self.frames[key] = nil
				if key == self.target then target_evicted = true end
			end
		end
		local order = {}
		for _, key in ipairs(self.order) do if self.frames[key] then order[#order + 1] = key end end
		self.order = order
		if self.target and self.last_time and (not cached(self.last_time) or target_evicted
			or (self.active and self.active.key == self.target and not cached(self.active.time))) then
			self:clear()
			if not cached(self.last_time) then status('outside-cache', self.last_time) end
		end
	end

	function self:request(time, w, h)
		if not finite(time) or not cached(time) then
			self:clear()
			status('outside-cache', time)
			return false
		end
		-- Half-second bins bound work while moving across the timeline. Store
		-- the original time as well so a shrinking cache invalidates old frames.
		local key = tostring(math.floor(time * 2)) .. ':' .. w .. 'x' .. h
		local changed = self.target ~= key
		self.target, self.last_time = key, time
		local frame = self.frames[key]
		if frame and cached(frame.time) then
			on_frame(frame.data, frame.w, frame.h)
			status('ready', time)
			return true
		end
		if changed then on_clear() end
		status('loading', time)
		local job = {key = key, time = time, w = w, h = h, generation = self.generation}
		if self.active then
			if self.active.key ~= key then self.pending = job end
		else
			start_job(job)
		end
		return true
	end
	return self
end

return M
