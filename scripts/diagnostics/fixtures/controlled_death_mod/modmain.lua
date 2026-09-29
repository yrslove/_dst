AddPrefabPostInit("world", function(world)
    if not world.ismastersim then
        return
    end

    world:ListenForEvent("ms_playerjoined", function(_, player)
        world:DoTaskInTime(60, function()
            if not player:IsValid() or player:HasTag("playerghost") then
                print("[controlled_death_fixture] ALREADY_DEAD_OR_ABSENT")
                return
            end

            local health = player.components.health
            if health == nil or health:IsDead() then
                print("[controlled_death_fixture] ALREADY_DEAD_OR_ABSENT")
                return
            end

            local x, _, z = player.Transform:GetWorldPosition()
            local machines = 0
            for _, entity in ipairs(TheSim:FindEntities(x, 0, z, 100)) do
                if entity:IsValid() and entity.prefab == "researchlab" then
                    machines = machines + 1
                end
            end
            if machines ~= 1 then
                print("[controlled_death_fixture] ABORT machine_count=" .. machines)
                return
            end

            print("[controlled_death_fixture] ALIVE_BASELINE machine_count=1")
            health:Kill()
            print("[controlled_death_fixture] DEATH_TRIGGERED")
        end)
    end)
end)
