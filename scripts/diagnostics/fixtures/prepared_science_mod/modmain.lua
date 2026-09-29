local function count_machines(x, z)
    local count = 0
    for _, entity in ipairs(TheSim:FindEntities(x, 0, z, 100)) do
        if entity:IsValid() and entity.prefab == "researchlab" then
            count = count + 1
        end
    end
    return count
end

print("[prepared_science_fixture] modmain loaded")

AddPrefabPostInit("world", function(world)
    print("[prepared_science_fixture] world callback master=" .. tostring(world.ismastersim))
    if not world.ismastersim then
        return
    end

    local function prepare(player)
        if not player:IsValid() or player:HasTag("playerghost") then
            print("[prepared_science_fixture] SKIP player is absent or dead")
            return
        end

        local x, _, z = player.Transform:GetWorldPosition()
        local before = count_machines(x, z)
        if before ~= 0 then
            print("[prepared_science_fixture] SKIP existing_count=" .. before)
            return
        end
        local candidates = {{x + 4, z + 2}, {x - 4, z + 2}, {x + 4, z - 2}}
        for _, point in ipairs(candidates) do
            if world.Map:IsPassableAtPoint(point[1], 0, point[2]) then
                local machine = GLOBAL.SpawnPrefab("researchlab")
                if machine ~= nil then
                    machine.Transform:SetPosition(point[1], 0, point[2])
                    print(string.format(
                        "[prepared_science_fixture] RESULT_OK before=%d after=%d guid=%s prefab=researchlab x=%.3f z=%.3f",
                        before, count_machines(x, z), tostring(machine.GUID), point[1], point[2]
                    ))
                    world:DoTaskInTime(1, function()
                        world:PushEvent("ms_save")
                        print("[prepared_science_fixture] SAVE_REQUESTED")
                    end)
                    return
                end
            end
        end
        print("[prepared_science_fixture] RESULT_FAILED no passable placement")
    end

    world:ListenForEvent("ms_playerjoined", function(_, player)
        world:DoTaskInTime(2, function()
            prepare(player)
        end)
    end)
end)
