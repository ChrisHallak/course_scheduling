from pydantic import BaseModel, Field
from typing import List


class Group(BaseModel):
    id: str
    instructorId: str
    courseId: str
    code: str
    sessions: int
    instructor: str
    course: str
    type: int = Field(..., description="0 = THEORY, 1 = LAB")


class SlotItem(BaseModel):
    dayId: str
    dayName: str
    timeId: str
    timeName: str


class AvailabilityItem(BaseModel):
    teacher: str
    teacherId: str
    slots: List[SlotItem]
    maxHours: int = Field(..., description="Maximum allowed teaching hours for this teacher")


class DayItem(BaseModel):
    Name: str
    Id: str


class TimeItem(BaseModel):
    Name: str
    Id: str


class ScheduleRequest(BaseModel):
    groups: List[Group]
    availability: List[AvailabilityItem]  # list of dicts (objects)
    days: List[DayItem]  # list of {Name, Id}
    time_intervals: List[TimeItem]  # list of {Name, Id}
    max_courses_per_slot: int


class ScheduledGroup(BaseModel):
    id: str
    code: str
    slot: SlotItem
    students_count: int
    type: int = Field(..., description="0 = THEORY, 1 = LAB")


class Room(BaseModel):
    id: str
    course_type: int = Field(..., description="0 = THEORY, 1 = LAB")
    capacity: int = Field(..., description="Maximum capacity for this room")


class DistributeGroupsRequest(BaseModel):
    scheduled_groups: List[ScheduledGroup]
    rooms: List[Room]
