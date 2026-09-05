import EventKit
import Foundation

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
store.requestFullAccessToEvents { ok, _ in granted = ok; sem.signal() }
_ = sem.wait(timeout: .now() + 10)
guard granted else { print("(calendar access not granted)"); exit(0) }

let cal = Calendar.current
let start = cal.startOfDay(for: Date())
let end = cal.date(byAdding: .day, value: 1, to: start)!
let events = store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: nil))
    .sorted { $0.startDate < $1.startDate }

if events.isEmpty { print("(no events today)"); exit(0) }
let fmt = DateFormatter(); fmt.dateFormat = "HH:mm"
for e in events {
    let who = (e.attendees ?? []).compactMap { $0.name }.prefix(6).joined(separator: ", ")
    let loc = [e.location, e.url?.absoluteString].compactMap { $0 }.first ?? ""
    print("\(fmt.string(from: e.startDate))–\(fmt.string(from: e.endDate))  \(e.title ?? "(untitled)")"
        + (who.isEmpty ? "" : "  · with \(who)")
        + (loc.isEmpty ? "" : "  · \(loc)"))
}
