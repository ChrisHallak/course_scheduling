from typing import Tuple
import time
from collections import defaultdict
from fastapi import FastAPI, HTTPException
from ortools.sat.python import cp_model
from typing import List, Dict
from models_schema import ScheduleRequest, DistributeGroupsRequest

app = FastAPI(title="Course Scheduler")

# =========================
# Health
# =========================

@app.get("/health")
async def health():
    return {"message": "healthy"}

@app.post("/create_schedule")
async def create_schedule(request: ScheduleRequest):
    print(request.model_dump())  # Pydantic v2; use .dict() if v1
    start_time = time.time()

    # -----------------------
    # Build lookup maps
    # -----------------------
    day_id_to_name: Dict[str, str] = {d.Id: d.Name for d in request.days}
    time_id_to_name: Dict[str, str] = {t.Id: t.Name for t in request.time_intervals}

    # All (dayId, timeId) combinations available in the calendar
    time_slots: List[Tuple[str, str]] = [(d.Id, t.Id) for d in request.days for t in request.time_intervals]

    # Convert availability list -> dict for easier lookup: { teacher: [(dayId,timeId), ...] }
    availability_dict: Dict[str, List[Tuple[str, str]]] = {
        a.teacher: [(s.dayId, s.timeId) for s in a.slots] for a in request.availability
    }

    max_hours_dict: Dict[str, int] = {
        a.teacher: a.maxHours for a in request.availability
    }

    groups = request.groups
    MAX_COURSES_PER_SLOT = request.max_courses_per_slot

    # Quick validation: ensure every instructor appears in availability
    missing_teachers = sorted({g.instructor for g in groups} - set(availability_dict.keys()))
    if missing_teachers:
        raise HTTPException(
            status_code=400,
            detail=f"Instructors missing in availability: {', '.join(missing_teachers)}",
        )

    # -----------------------
    # Validation checks
    # -----------------------
    errors = []

    # Count sessions per instructor
    sessions_per_instructor = {}
    for g in request.groups:
        sessions_per_instructor[g.instructorId] = sessions_per_instructor.get(g.instructorId, 0) + g.sessions

    # Build availability map
    availability_map = {a.teacherId: a for a in request.availability}

    for instructor_id, total_sessions in sessions_per_instructor.items():
        if instructor_id not in availability_map:
            errors.append(f"Instructor {instructor_id} has assigned groups but no availability defined.")
            continue

        availability = availability_map[instructor_id]

        # Rule 1: available slots < assigned sessions
        if len(availability.slots) < total_sessions:
            errors.append(
                f"Instructor {availability.teacher} ({instructor_id}) "
                f"has {total_sessions} sessions but only {len(availability.slots)} slots."
            )

        # Rule 2: maxHours == 0 but sessions assigned
        if availability.maxHours == 0 and total_sessions > 0:
            errors.append(
                f"Instructor {availability.teacher} with id : ({instructor_id}) "
                f"has max Hours=0 but is assigned {total_sessions} sessions."
            )

        # Rule 3: sessions > maxHours
        if availability.maxHours > 0 and total_sessions > availability.maxHours:
            errors.append(
                f"Instructor {availability.teacher} ({instructor_id}) "
                f"is assigned {total_sessions} sessions but only allowed maxHours={availability.maxHours}."
            )

    if errors:
        raise HTTPException(status_code=400, detail=errors)



    # CP-SAT model
    model = cp_model.CpModel()

    # Decision variables: x[(groupId, session_index, dayId, timeId)] ∈ {0,1}
    x: Dict[Tuple[str, int, str, str], cp_model.IntVar] = {}
    for g in groups:
        for s_idx in range(g.sessions):
            for day_id, time_id in time_slots:
                x[(g.id, s_idx, day_id, time_id)] = model.NewBoolVar(
                    f"x_{g.id}_{s_idx}_{day_id}_{time_id}"
                )

    # -----------------------
    # Constraints
    # -----------------------

    # 1) Each session must be scheduled exactly once
    for g in groups:
        for s_idx in range(g.sessions):
            model.Add(sum(x[(g.id, s_idx, d, t)] for d, t in time_slots) == 1)

    # 2) Instructor cannot teach two courses at the same time
    for inst in {g.instructor for g in groups}:
        for d, t in time_slots:
            model.Add(
                sum(
                    x[(g.id, s_idx, d, t)]
                    for g in groups if g.instructor == inst
                    for s_idx in range(g.sessions)
                ) <= 1
            )

    # 3) Instructor availability constraint (based on dayId/timeId)
    for g in groups:
        inst = g.instructor
        allowed_slots = set(availability_dict.get(inst, []))
        for s_idx in range(g.sessions):
            for d, t in time_slots:
                if (d, t) not in allowed_slots:
                    model.Add(x[(g.id, s_idx, d, t)] == 0)

    # 4) No two groups for the same course can overlap (use courseId)
    course_ids = {g.courseId for g in groups}
    for cid in course_ids:
        related = [g for g in groups if g.courseId == cid]
        for d, t in time_slots:
            model.Add(
                sum(
                    x[(rg.id, s_idx, d, t)]
                    for rg in related
                    for s_idx in range(rg.sessions)
                ) <= 1
            )

    # 5) Maximum number of simultaneous courses per slot
    for d, t in time_slots:
        model.Add(
            sum(
                x[(g.id, s_idx, d, t)]
                for g in groups
                for s_idx in range(g.sessions)
            ) <= MAX_COURSES_PER_SLOT
        )

    # 6) Maximum hours per teacher if you later re-enable it
    for inst in {g.instructor for g in groups}:
        max_hours = max_hours_dict.get(inst, None)
        if max_hours is not None:
            model.Add(
                sum(
                    (2 if g.type == 0 else 1) * x[(g.id, s_idx, d, t)]
                    for g in groups if g.instructor == inst
                    for s_idx in range(g.sessions)
                    for d, t in time_slots
                ) <= max_hours
            )

    # -----------------------
    # Solve
    # -----------------------
    solver = cp_model.CpSolver()
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise HTTPException(status_code=400, detail="No feasible schedule found.")

    # -----------------------
    # Collect solution
    # -----------------------
    schedule = []
    for g in groups:
        for s_idx in range(g.sessions):
            for d, t in time_slots:
                if solver.Value(x[(g.id, s_idx, d, t)]) == 1:
                    schedule.append(
                        {
                            "GroupId": g.id,
                            "CourseCode": g.code,
                            "CourseId": g.courseId,
                            "CourseFamily": g.course,
                            "Session": s_idx + 1,
                            "Instructor": g.instructor,
                            "InstructorId": g.instructorId,
                            "DayId": d,
                            "Day": day_id_to_name.get(d, d),
                            "TimeId": t,
                            "Time": time_id_to_name.get(t, t),
                            "Type": g.type,
                        }
                    )

    return {
        "schedule": schedule,
        # "execution_time_seconds": round(time.time() - start_time, 3),
    }


@app.post("/distribute_groups")
async def distribute_groups(request: DistributeGroupsRequest):
    scheduled_groups = request.scheduled_groups
    rooms = request.rooms

    slot_to_groups = defaultdict(list)
    for g in scheduled_groups:
        slot = (g.slot.dayId, g.slot.timeId)
        slot_to_groups[slot].append(g)

    final_distribution = []

    for slot, groups_in_slot in slot_to_groups.items():
        day, time = slot
        model = cp_model.CpModel()

        # Create variables: y[(group_id, room_id)] = 1 if group is assigned to this room
        y = {}
        for g in groups_in_slot:
            for r in rooms:
                if r.capacity >= g.students_count and r.course_type == g.type:
                    y[(g.id, r.id)] = model.NewBoolVar(f"y_{g.id}_{r.id}")

        # Constraint 1: each group assigned to exactly one room
        for g in groups_in_slot:
            possible_rooms = [y[(g.id, r.id)] for r in rooms if (g.id, r.id) in y]
            model.Add(sum(possible_rooms) == 1)

        # Constraint 2: each room holds at most one group at a time
        for r in rooms:
            assigned_groups = [y[(g.id, r.id)] for g in groups_in_slot if (g.id, r.id) in y]
            model.Add(sum(assigned_groups) <= 1)

        # Solve
        solver = cp_model.CpSolver()
        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise HTTPException(
                status_code=400,
                detail=f"No feasible room assignment for slot {day}-{time}"
            )

        # Collect assignments and attach roomId to each scheduled group
        for g in groups_in_slot:
            assigned_room = next(
                r.id for r in rooms if (g.id, r.id) in y and solver.Value(y[(g.id, r.id)]) == 1
            )
            final_distribution.append({
                "id": g.id,
                "slot": g.slot,
                "number_size": g.students_count,
                "type": g.type,
                "roomId": assigned_room  # new field
            })

    return {"scheduled_groups": final_distribution}

